"""Persistent repair execution and CLI approval across process restarts."""

import os
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .config import Settings
from .docker_runner import DockerTestRunner
from .github import RealGitHubClient
from .graph import build_graph
from .llm import RealLLMClient
from .reporting import approval_summary, result_payload
from .state import RepairState
from .storage import JobStore


class RepairService:
    def __init__(self, settings: Settings, *, adapter_factory=None):
        self.settings = settings
        self.store = JobStore(settings.data_dir)
        self.adapter_factory = adapter_factory or self._adapters

    def _adapters(self):
        # Check configuration before constructing clients that hold secret credentials.
        if not self.settings.openai_api_key.get_secret_value() or not self.settings.openai_model:
            raise ValueError("Configure OPENAI_API_KEY and PATCHPILOT_OPENAI_MODEL")
        github = RealGitHubClient(self.settings)
        try:
            return github, RealLLMClient(self.settings, cache=self.store), DockerTestRunner(self.settings)
        except Exception:
            github.close()
            raise

    def repair(self, repo, run_id):
        self.settings.allow_repository(repo)
        github = RealGitHubClient(self.settings)
        try:
            failure = github.load_failure(repo, run_id)
        finally:
            github.close()
        job_id, _ = self.store.enqueue(repo, run_id, failure.run_attempt, failure.head_sha)
        return self.execute(job_id)

    def execute(self, job_id, *, resume=None):
        with self.store.lock(job_id):
            job = self.store.get(job_id)
            publication_retry = resume is True and job["status"] == "PUBLISH_FAILED"
            if resume is not None and job["status"] != "AWAITING_APPROVAL" and not publication_retry:
                # Repeated approve/reject is a read, never a second publish.
                if job["status"] in {"CI_PENDING", "CI_VERIFIED", "CI_FAILED", "REJECTED"}:
                    return self.status(job_id, refresh=False)
                raise ValueError("Job is not awaiting approval")
            if resume is None and job["status"] not in {"QUEUED", "RUNNING"}:
                return self.status(job_id, refresh=False)
            github = None
            try:
                github, llm, runner = self.adapter_factory()
                checkpoint_path = self.settings.data_dir / "checkpoints.sqlite3"
                descriptor = os.open(checkpoint_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                os.close(descriptor)
                config = {"configurable": {"thread_id": job_id}, "recursion_limit": 50}
                with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                    graph = build_graph(github, llm, runner, checkpointer=saver, publisher=github)
                    snapshot = graph.get_state(config)
                    if publication_retry:
                        state = RepairState.model_validate(snapshot.values)
                        if not state.approved or state.status != "publish_failed":
                            raise ValueError("Publication retry requires a previously approved checkpoint")
                        graph.update_state(config, {"status": "approved", "error": None}, as_node="request_approval")
                        graph.invoke(None, config)
                    elif resume is not None:
                        if not snapshot.tasks or not any(task.interrupts for task in snapshot.tasks):
                            raise ValueError("Approval checkpoint is missing")
                        graph.invoke(Command(resume=resume), config)
                    else:
                        self.store.save(job_id, "RUNNING", job["result"])
                        if snapshot.values:
                            if snapshot.next:
                                graph.invoke(None, config)
                        else:
                            request = job["request"]
                            failure = github.load_failure(request["repo"], request["run_id"])
                            if failure.run_attempt != request["run_attempt"] or (
                                request["head_sha"] and request["head_sha"] != failure.head_sha
                            ):
                                raise ValueError("Workflow attempt/commit changed since submission")
                            state = RepairState(failure=failure, simulated=False)
                            graph.invoke(state.model_dump(), config)
                    snapshot = graph.get_state(config)
                    state = RepairState.model_validate(snapshot.values)
                    awaiting = any(task.interrupts for task in snapshot.tasks)
                    payload = result_payload(state, awaiting_approval=awaiting)
                    payload["metrics"]["total_seconds"] = time.time() - job["created"]
                    self.store.save(job_id, payload["status"], payload)
                    return self.status(job_id, refresh=False)
            except Exception as exc:
                # Adapter errors expose bounded messages; unknown exception details may contain secrets.
                from .github import GitHubError
                from .docker_runner import SandboxError
                from .llm import LLMError

                safe = str(exc) if isinstance(exc, (ValueError, GitHubError, SandboxError, LLMError)) else type(exc).__name__
                if resume is not None:
                    payload = {**job["result"], "status": job["status"], "operation_error": safe}
                    self.store.save(job_id, job["status"], payload)
                    return self.status(job_id, refresh=False)
                payload = {
                    "status": "MANUAL_ACTION_REQUIRED", "simulated": False, "error": safe,
                    "metrics": {"total_seconds": time.time() - job["created"], "active_seconds": 0,
                                "llm_calls": 0, "cached_llm_calls": 0, "input_tokens": 0, "output_tokens": 0,
                                "patch_attempts": 0, "changed_files": 0, "local_tests_passed": False, "pr_url": None},
                }
                self.store.save(job_id, payload["status"], payload)
                return self.status(job_id, refresh=False)
            finally:
                if github is not None and hasattr(github, "close"):
                    github.close()

    def status(self, job_id, *, refresh=True):
        job = self.store.get(job_id)
        result = job["result"] or {"status": job["status"], "simulated": False}
        if refresh and job["status"] in {"CI_PENDING", "CI_VERIFIED", "CI_FAILED"}:
            with self.store.lock(job_id):
                state = RepairState.model_validate(result["state"])
                github = RealGitHubClient(self.settings)
                try:
                    status = github.ci_status(state)
                finally:
                    github.close()
                state = state.model_copy(update={"status": status})
                state = state.model_copy(update={"report": approval_summary(state)})
                updated = result_payload(state)
                updated["metrics"]["total_seconds"] = result["metrics"].get("total_seconds", 0)
                self.store.save(job_id, updated["status"], updated)
                result = updated
        return {"job_id": job_id, "thread_id": job_id, **result}
