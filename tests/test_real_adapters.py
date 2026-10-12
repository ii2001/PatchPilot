import base64
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from patchpilot.config import Settings
from patchpilot.github import GitHubError, RealGitHubClient
from patchpilot.llm import LLMError, RealLLMClient
from patchpilot.policy import workspace_digest
from patchpilot.scenarios import run_scenario
from patchpilot.state import FailureInfo, TestExecution as Execution, TestResult as Result
from patchpilot.storage import JobStore


def verified_state():
    """Synthetic Docker evidence for API contract tests, never a live validation claim."""
    state = run_scenario("success")
    failure = FailureInfo(
        repository="owner/repo", run_id="123", log="assertion", head_sha="a" * 40,
        base_branch="main", tree_sha="b" * 40, workflow_id=42, run_url="https://github.com/owner/repo/actions/runs/123",
        created_at="2026-10-12T00:00:00Z", modes={path: "100644" for path in state.files},
        blobs={path: hashlib.sha1(f"blob {len(content.encode())}\0".encode() + content.encode()).hexdigest()
               for path, content in state.original_files.items()},
    )
    def evidence(files, passed):
        return Result(
            outcome="passed" if passed else "failed", output="synthetic Docker contract evidence",
            simulated=False, runner="docker", workspace_digest=workspace_digest(files),
            executions=[Execution(
                stage=stage, command=["python", "-m", "pytest"], exit_code=0 if passed else 1,
                output="synthetic", collected=1, passed=int(passed), failed=int(not passed),
                node_ids=["tests/test_calculator.py::test_add"],
            ) for stage in ("targeted", "full")],
        )
    return state.model_copy(update={
        "failure": failure, "simulated": False, "approved": True, "status": "approved",
        "reproduction": evidence(state.original_files, False), "test_results": [evidence(state.files, True)],
    })


def test_github_load_failure_and_pinned_blob():
    source = b"def add(a, b):\n    return a - b\n"
    sha = hashlib.sha1(f"blob {len(source)}\0".encode() + source).hexdigest()
    def transport(request):
        path = request.url.path
        if path.endswith("/actions/runs/123"):
            body = dict(status="completed", conclusion="failure", head_repository={"full_name": "owner/repo"},
                        head_branch="main", head_sha="a" * 40, workflow_id=42, html_url="https://github.com/run",
                        created_at="2026-10-12T00:00:00Z", run_attempt=2)
        elif path.endswith("/attempts/2/jobs"):
            body = {"jobs": [{"id": 99, "name": "pytest", "conclusion": "failure",
                               "steps": [{"name": "test", "conclusion": "failure"}]}]}
        elif path.endswith("/logs"):
            return httpx.Response(200, text="AssertionError in tests/test_app.py")
        elif path.endswith("/git/commits/" + "a" * 40):
            body = {"tree": {"sha": "b" * 40}}
        elif "/git/trees/" in path:
            body = {"truncated": False, "tree": [{"path": "src/app.py", "type": "blob", "mode": "100644", "sha": sha, "size": len(source)}]}
        else:
            assert path.endswith(sha)
            body = {"encoding": "base64", "content": base64.b64encode(source).decode()}
        return httpx.Response(200, json=body)

    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(transport)) as client:
        github = RealGitHubClient(Settings(allowed_repos={"owner/repo"}), client=client)
        failure = github.load_failure("owner/repo", "123")
        assert failure.head_sha == "a" * 40 and failure.run_attempt == 2
        assert failure.failed_jobs[0]["steps"][0]["name"] == "test"
        assert github.collect_context(failure) == {"src/app.py": source.decode()}


@pytest.mark.parametrize("problem", ["sha", "binary"])
def test_rejects_unverified_or_binary_blob(problem):
    data = b"\xff" if problem == "binary" else b"tampered"
    sha = hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest() if problem == "binary" else "a" * 40
    client = httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(data).decode()})
    ))
    with client:
        github = RealGitHubClient(Settings(allowed_repos={"owner/repo"}), client=client)
        with pytest.raises(GitHubError):
            github.collect_context(FailureInfo(repository="owner/repo", run_id="1", log="failed", blobs={"app.py": sha}))


@pytest.mark.parametrize("case", ["no_approval", "mock", "moved_base", "branch_collision"])
def test_publication_fail_closed(case):
    state = verified_state()
    if case == "no_approval":
        state = state.model_copy(update={"approved": False})
    if case == "mock":
        state = state.model_copy(update={"simulated": True})
    writes = []
    def transport(request):
        path = request.url.path
        if request.method == "POST":
            writes.append(path)
            return httpx.Response(201, json={"sha": "c" * 40})
        sha = "d" * 40 if case in {"moved_base", "branch_collision"} else "a" * 40
        if "patchpilot/" not in path and case == "branch_collision":
            sha = "a" * 40
        return httpx.Response(200, json={"object": {"sha": sha}})
    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(transport)) as client:
        github = RealGitHubClient(Settings(allowed_repos={"owner/repo"}), client=client)
        with pytest.raises(GitHubError):
            github.create_draft_pr(state)
    if case != "branch_collision":
        assert not writes
    assert not any(path.endswith("/pulls") or path.endswith("/refs") for path in writes)


def test_draft_pr_is_idempotent_and_carries_evidence():
    state = verified_state()
    reference = False
    pull = None
    writes = []
    def transport(request):
        nonlocal reference, pull
        path = request.url.path
        if request.method == "POST":
            body = json.loads(request.content)
            writes.append((path, body))
            if path.endswith("/refs"):
                reference = body["ref"]
                return httpx.Response(201, json={})
            if path.endswith("/pulls"):
                assert body["draft"] is True
                assert "Original run:" in body["body"] and "Docker" in body["body"]
                assert "a/src/calculator.py" in body["body"]
                pull = {"html_url": "https://github.com/owner/repo/pull/1"}
                return httpx.Response(201, json=pull)
            if path.endswith("/commits"):
                assert body["parents"] == [state.failure.head_sha]
                assert body["author"]["date"] == body["committer"]["date"]
                assert body["message"].startswith("🐛 fix(ci):") and "검증:" in body["message"]
            return httpx.Response(201, json={"sha": "c" * 40})
        if path.endswith("/pulls"):
            matching = reference and request.url.params["head"].endswith(reference.removeprefix("refs/heads/"))
            return httpx.Response(200, json=[pull] if pull and matching else [])
        if "patchpilot/" in path:
            matching = reference and path.endswith(reference.removeprefix("refs/heads/"))
            return httpx.Response(200 if matching else 404, json={"object": {"sha": "c" * 40}})
        return httpx.Response(200, json={"object": {"sha": "a" * 40}})
    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(transport)) as client:
        github = RealGitHubClient(Settings(allowed_repos={"owner/repo"}), client=client)
        first = github.create_draft_pr(state)
        assert github.create_draft_pr(state) == first
        rerun = state.model_copy(update={"failure": state.failure.model_copy(update={"run_attempt": 2})})
        assert github.create_draft_pr(rerun) == first
    assert sum(path.endswith("/pulls") for path, _ in writes) == 1
    assert sum(path.endswith("/refs") for path, _ in writes) == 1


@pytest.mark.parametrize("conclusion,expected", [("success", "ci_verified"), ("failure", "ci_failed"), (None, "ci_pending")])
def test_remote_ci_is_separate_from_local(conclusion, expected):
    state = verified_state().model_copy(update={"published_sha": "c" * 40})
    run = dict(id=100, workflow_id=42, head_sha="c" * 40, status="completed" if conclusion else "in_progress", conclusion=conclusion)
    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"workflow_runs": [run]})
    )) as client:
        assert RealGitHubClient(Settings(), client=client).ci_status(state) == expected


class StructuredModel:
    def __init__(self, *, problem=None):
        self.problem = problem
        self.payloads = []

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "json_schema", "include_raw": True}
        self.schema = schema
        return self

    def invoke(self, messages):
        self.payloads.append(json.loads(messages[1][1]))
        if self.problem == "timeout":
            raise TimeoutError("secret-token-should-not-be-stored")
        if self.problem == "api":
            raise httpx.ConnectError("secret-token-should-not-be-stored")
        if self.problem == "malformed":
            return {"parsed": None, "parsing_error": ValueError("invalid"), "raw": None}
        if self.schema.__name__ == "Diagnosis":
            parsed = {"cause": "subtraction", "suggested_fix": "addition", "category": "code", "evidence": ["assertion"]}
        else:
            parsed = {"category": "code", "reason": "assertion"}
        return {"parsed": parsed, "raw": SimpleNamespace(usage_metadata={"input_tokens": 10, "output_tokens": 5})}


def test_llm_structured_output_records_usage_and_retry_context(tmp_path):
    model = StructuredModel()
    client = RealLLMClient(Settings(openai_model="configured-model"), model=model, cache=JobStore(tmp_path))
    state = run_scenario("retry_success")
    assert client.diagnose(state).category == "code"
    payload = model.payloads[0]
    assert payload["previous_results"][-1]["output"] == state.test_results[-1].output
    assert len(payload["previous_patches"]) == 2
    assert client.calls[0].input_tokens == 10 and client.calls[0].output_tokens == 5
    assert client.diagnose(state).cause == "subtraction"
    assert len(model.payloads) == 1  # Durable response cache prevents normal replay calls.


@pytest.mark.parametrize("problem", ["timeout", "api", "malformed"])
def test_llm_failures_are_sanitized_and_recorded(problem):
    client = RealLLMClient(Settings(), model=StructuredModel(problem=problem))
    with pytest.raises(LLMError) as error:
        client.diagnose(run_scenario("success"))
    assert "secret-token" not in str(error.value)
    assert client.calls[0].error


def test_missing_llm_credentials_is_explicit():
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        RealLLMClient(Settings())


def test_signed_log_redirect_never_follows_untrusted_host():
    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "https://attacker.example/log"})
    )) as client:
        github = RealGitHubClient(Settings(), client=client)
        with pytest.raises(GitHubError, match="download host"):
            github._job_log("owner/repo", 1)


@pytest.mark.parametrize("mode,kind", [("120000", "blob"), ("160000", "commit")])
def test_github_rejects_symlinks_and_submodules(mode, kind):
    def transport(request):
        path = request.url.path
        if path.endswith("/actions/runs/123"):
            body = dict(status="completed", conclusion="failure", head_repository={"full_name": "owner/repo"},
                        head_branch="main", head_sha="a" * 40, workflow_id=42, html_url="url", created_at="date")
        elif path.endswith("/jobs"):
            body = {"jobs": [{"id": 1, "name": "test", "conclusion": "failure"}]}
        elif path.endswith("/logs"):
            return httpx.Response(200, text="failure")
        elif "/commits/" in path:
            body = {"tree": {"sha": "b" * 40}}
        else:
            body = {"tree": [{"path": "src/link", "mode": mode, "type": kind, "sha": "c" * 40}]}
        return httpx.Response(200, json=body)
    with httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(transport)) as client:
        github = RealGitHubClient(Settings(allowed_repos={"owner/repo"}), client=client)
        with pytest.raises(GitHubError, match="Symlinks and submodules"):
            github.load_failure("owner/repo", "123")


@pytest.mark.parametrize("timeout", [False, True])
def test_real_langchain_openai_sdk_over_mock_http(timeout):
    from langchain_openai import ChatOpenAI

    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        if timeout:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return httpx.Response(200, json={
            "id": "chatcmpl-test", "object": "chat.completion", "created": 1, "model": "test-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps({
                "cause": "incorrect subtraction", "suggested_fix": "use addition", "category": "code", "evidence": ["AssertionError"],
            })}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
        })
    with httpx.Client(transport=httpx.MockTransport(transport)) as http_client:
        model = ChatOpenAI(model="test-model", api_key="synthetic-test-key", http_client=http_client, max_retries=0)
        client = RealLLMClient(Settings(openai_model="test-model"), model=model)
        if timeout:
            with pytest.raises(LLMError, match="TimeoutError"):
                client.diagnose(run_scenario("success"))
        else:
            assert client.diagnose(run_scenario("success")).cause == "incorrect subtraction"
            assert client.calls[0].input_tokens == 10 and client.calls[0].output_tokens == 8
    assert len(requests) == 1 and requests[0]["response_format"]["type"] == "json_schema"
