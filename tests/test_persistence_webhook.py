import hashlib
import hmac
import json

from fastapi.testclient import TestClient
import pytest

from patchpilot.adapters import FakeGitHubClient, FakeLLMClient
from patchpilot.config import Settings
from patchpilot.service import RepairService
from patchpilot.storage import JobBusyError, JobStore
from patchpilot.webhook import create_app
from test_real_adapters import verified_state


def test_approval_survives_restart_and_publishes_once(tmp_path, monkeypatch):
    monkeypatch.setenv("LANGGRAPH_STRICT_MSGPACK", "true")
    evidence = verified_state()
    published = []
    class GitHub(FakeGitHubClient):
        def load_failure(self, repo, run_id):
            return evidence.failure

        def create_draft_pr(self, state):
            assert state.approved and not state.simulated
            published.append(state)
            return {"pr_url": "https://github.com/owner/repo/pull/1", "published_sha": "c" * 40}

    class Runner:
        def run(self, files, *, phase, attempt):
            return evidence.reproduction if phase == "reproduce" else evidence.test_results[-1]

    settings = Settings(data_dir=tmp_path, allowed_repos={"owner/repo"})
    factory = lambda: (GitHub(), FakeLLMClient(), Runner())
    service = RepairService(settings, adapter_factory=factory)
    job_id, _ = service.store.enqueue("owner/repo", "123", head_sha="a" * 40)
    waiting = service.execute(job_id)
    assert waiting["status"] == "AWAITING_APPROVAL"
    assert waiting["verification_level"] == "LOCAL_DOCKER" and waiting["diff"]
    assert not published
    restored = RepairService(settings, adapter_factory=factory)
    assert restored.status(job_id, refresh=False)["status"] == "AWAITING_APPROVAL"
    restored.store.save(job_id, "RUNNING", waiting)  # Simulate crash before interrupt status was recorded.
    assert restored.execute(job_id)["status"] == "AWAITING_APPROVAL"
    approved = restored.execute(job_id, resume=True)
    assert approved["status"] == "CI_PENDING" and approved["metrics"]["pr_url"]
    assert len(published) == 1
    assert restored.execute(job_id, resume=True)["status"] == "CI_PENDING"
    assert len(published) == 1


def test_rejected_approval_never_publishes(tmp_path):
    evidence = verified_state()
    class GitHub(FakeGitHubClient):
        def load_failure(self, repo, run_id):
            return evidence.failure

        def create_draft_pr(self, state):
            pytest.fail("Rejected repair must never publish")

    class Runner:
        def run(self, files, *, phase, attempt):
            return evidence.reproduction if phase == "reproduce" else evidence.test_results[-1]

    service = RepairService(Settings(data_dir=tmp_path), adapter_factory=lambda: (GitHub(), FakeLLMClient(), Runner()))
    job_id, _ = service.store.enqueue("owner/repo", "123", head_sha="a" * 40)
    assert service.execute(job_id)["status"] == "AWAITING_APPROVAL"
    assert RepairService(service.settings, adapter_factory=service.adapter_factory).execute(job_id, resume=False)["status"] == "REJECTED"


@pytest.mark.parametrize("failure_mode", ["publish", "configuration"])
def test_approval_retry_preserves_verified_checkpoint(tmp_path, failure_mode):
    from patchpilot.github import GitHubError

    evidence = verified_state()
    factory_calls, publishes, tests = [], [], []
    class GitHub(FakeGitHubClient):
        def load_failure(self, repo, run_id):
            return evidence.failure

        def create_draft_pr(self, state):
            publishes.append(state)
            if failure_mode == "publish" and len(publishes) == 1:
                raise GitHubError("Simulated response loss after branch creation")
            return {"pr_url": "https://github.com/owner/repo/pull/1", "published_sha": "c" * 40}

    class Runner:
        def run(self, files, *, phase, attempt):
            tests.append((phase, attempt))
            return evidence.reproduction if phase == "reproduce" else evidence.test_results[-1]

    def factory():
        factory_calls.append(True)
        if failure_mode == "configuration" and len(factory_calls) == 2:
            raise ValueError("Missing credentials after restart")
        return GitHub(), FakeLLMClient(), Runner()

    settings = Settings(data_dir=tmp_path)
    service = RepairService(settings, adapter_factory=factory)
    job_id, _ = service.store.enqueue("owner/repo", "123", head_sha="a" * 40)
    assert service.execute(job_id)["status"] == "AWAITING_APPROVAL"
    first = service.execute(job_id, resume=True)
    assert first["status"] == ("PUBLISH_FAILED" if failure_mode == "publish" else "AWAITING_APPROVAL")
    restored = RepairService(settings, adapter_factory=factory)
    assert restored.execute(job_id, resume=True)["status"] == "CI_PENDING"
    assert tests == [("reproduce", 0), ("repair", 1)]


def test_changed_commit_since_webhook_is_refused(tmp_path):
    evidence = verified_state()
    class GitHub(FakeGitHubClient):
        def load_failure(self, repo, run_id):
            return evidence.failure
    service = RepairService(Settings(data_dir=tmp_path), adapter_factory=lambda: (GitHub(), FakeLLMClient(), None))
    job_id, _ = service.store.enqueue("owner/repo", "123", head_sha="0" * 40)
    result = service.execute(job_id)
    assert result["status"] == "MANUAL_ACTION_REQUIRED"
    assert "changed" in result["error"]


def test_missing_config_is_durable_manual_action(tmp_path):
    service = RepairService(Settings(data_dir=tmp_path))
    job_id, _ = service.store.enqueue("owner/repo", "123")
    assert service.execute(job_id)["status"] == "MANUAL_ACTION_REQUIRED"
    assert service.store.get(job_id)["result"]["error"]


def test_job_lock_prevents_concurrent_execution(tmp_path):
    store = JobStore(tmp_path)
    job_id, _ = store.enqueue("owner/repo", "123")
    with store.lock(job_id):
        with pytest.raises(JobBusyError):
            with JobStore(tmp_path).lock(job_id):
                pass


def test_store_rejects_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        JobStore(link)


def event():
    return {"action": "completed", "repository": {"full_name": "owner/repo"}, "workflow_run": {
        "id": 123, "run_attempt": 1, "status": "completed", "conclusion": "failure",
        "head_sha": "a" * 40, "head_branch": "main", "head_repository": {"full_name": "owner/repo"},
    }}


def deliver(client, payload, *, signature=True, delivery="delivery-1"):
    body = json.dumps(payload).encode()
    digest = "sha256=" + hmac.new(b"test-secret", body, hashlib.sha256).hexdigest() if signature else "sha256=wrong"
    return client.post("/webhooks/github", content=body, headers={
        "x-hub-signature-256": digest, "x-github-event": "workflow_run", "x-github-delivery": delivery,
    })


def test_webhook_signed_durable_deduplication(tmp_path):
    settings = Settings(data_dir=tmp_path, webhook_secret="test-secret", allowed_repos={"owner/repo"})
    with TestClient(create_app(settings)) as client:
        first = deliver(client, event())
        assert first.status_code == 202 and first.json()["queued"]
        assert deliver(client, event()).json()["duplicate"]
        assert deliver(client, event(), delivery="delivery-2").json()["duplicate"]
        assert deliver(client, event(), signature=False).status_code == 401
    reopened = JobStore(tmp_path)
    assert len(reopened.pending()) == 1
    assert reopened.get(first.json()["job_id"])["status"] == "QUEUED"


@pytest.mark.parametrize("case", ["repo", "branch", "fork", "success", "malformed"])
def test_webhook_filters_unwanted_events(tmp_path, case):
    payload = event()
    if case == "repo":
        payload["repository"]["full_name"] = "other/repo"
    elif case == "branch":
        payload["workflow_run"]["head_branch"] = "patchpilot/repair-123"
    elif case == "fork":
        payload["workflow_run"]["head_repository"]["full_name"] = "fork/repo"
    elif case == "success":
        payload["workflow_run"]["conclusion"] = "success"
    else:
        del payload["workflow_run"]["head_sha"]
    with TestClient(create_app(Settings(data_dir=tmp_path, webhook_secret="test-secret", allowed_repos={"owner/repo"}))) as client:
        response = deliver(client, payload)
        assert response.status_code in {202, 400}
        assert not JobStore(tmp_path).pending()


def test_webhook_duplicate_delivery_with_new_body_is_rejected(tmp_path):
    settings = Settings(data_dir=tmp_path, webhook_secret="test-secret", allowed_repos={"owner/repo"})
    with TestClient(create_app(settings)) as client:
        assert deliver(client, event()).status_code == 202
        changed = event()
        changed["workflow_run"]["id"] = 124
        assert deliver(client, changed).status_code == 400
