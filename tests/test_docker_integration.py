"""Real sandbox executions; no fixture code is imported or run on the host."""

import os
from pathlib import Path
import subprocess

import pytest

from patchpilot.adapters import FakeLLMClient, FIXED_SOURCE, SOURCE_PATH
from patchpilot.config import Settings
from patchpilot.docker_runner import DockerTestRunner
from patchpilot.graph import build_graph
from patchpilot.state import FailureInfo, FileChange, Patch, RepairState

pytestmark = pytest.mark.docker


@pytest.fixture
def sandbox_settings():
    image = os.getenv("PATCHPILOT_SANDBOX_IMAGE", "")
    if os.getenv("PATCHPILOT_RUN_DOCKER_TESTS") != "1" or not image:
        pytest.skip("Set PATCHPILOT_RUN_DOCKER_TESTS=1 and immutable PATCHPILOT_SANDBOX_IMAGE")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("Docker daemon unavailable")
    return Settings(sandbox_image=image, test_targets=("tests/test_calculator.py",))


def fixture_files(name):
    root = Path(__file__).resolve().parents[1] / "examples" / "failing_pytest_demo" / name
    return {path.relative_to(root).as_posix(): path.read_text() for path in root.rglob("*.py")}


@pytest.mark.parametrize("scenario,fixture,status,attempts", [
    ("assertion", "assertion", "local_verified", 1),
    ("syntax", "syntax", "local_verified", 1),
    ("import", "import", "local_verified", 1),
    ("retry", "assertion", "local_verified", 2),
    ("max_failures", "assertion", "failed", 3),
    ("environment", "environment", "manual_action_required", 0),
    ("protected", "assertion", "failed", 1),
    ("not_reproduced", "assertion", "not_reproduced", 0),
])
def test_docker_repair_scenarios(sandbox_settings, scenario, fixture, status, attempts):
    files = fixture_files(fixture)
    if scenario == "not_reproduced":
        files[SOURCE_PATH] = FIXED_SOURCE
    if scenario == "environment":
        sandbox_settings = sandbox_settings.model_copy(update={"required_env": ("PATCHPILOT_DEMO_OFFSET",)})
    class GitHub:
        def collect_context(self, failure):
            return files.copy()

    class LLM(FakeLLMClient):
        def generate_patch(self, state):
            if scenario == "protected":
                path = "tests/test_calculator.py"
                return Patch(changes=[FileChange(path=path, before=state.files[path], after="pass\n")])
            return super().generate_patch(state)

    llm = LLM(None if scenario == "max_failures" else 2 if scenario == "retry" else 1)
    graph = build_graph(GitHub(), llm, DockerTestRunner(sandbox_settings))
    state = RepairState(failure=FailureInfo(repository="controlled/demo", run_id="fixture", log="fixture failure", head_sha="a" * 40), simulated=False)
    result = RepairState.model_validate(graph.invoke(state.model_dump(), {"recursion_limit": 50}))
    assert result.status == status, result.error or result.reproduction.output
    assert result.attempt == attempts
    assert not result.reproduction.simulated
    assert result.files["tests/test_calculator.py"] == files["tests/test_calculator.py"]
    if status == "local_verified":
        assert {execution.stage for execution in result.test_results[-1].executions} == {"targeted", "full"}


def test_container_is_nonprivileged_readonly_and_without_secrets(sandbox_settings, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-enter-container")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-enter-container")
    files = fixture_files("assertion")
    files[SOURCE_PATH] = FIXED_SOURCE
    files["tests/test_security.py"] = '''
import os
from pathlib import Path
import pytest

def test_isolation():
    assert os.geteuid() == 65532
    assert 'GITHUB_TOKEN' not in os.environ and 'OPENAI_API_KEY' not in os.environ
    assert not Path('/var/run/docker.sock').exists()
    with pytest.raises(OSError):
        Path('/workspace/src/calculator.py').write_text('tamper')
    with pytest.raises(OSError):
        Path('/opt/patchpilot/runner.py').write_text('tamper')
    assert len(Path('/proc/net/route').read_text().splitlines()) == 1
'''
    result = DockerTestRunner(sandbox_settings).run(files, phase="repair", attempt=1)
    assert result.outcome == "passed", result.output


def test_sandbox_timeout_and_cleanup(sandbox_settings):
    settings = sandbox_settings.model_copy(update={"test_timeout": 2})
    files = fixture_files("assertion")
    files[SOURCE_PATH] = "import time\ntime.sleep(30)\ndef add(a, b):\n    return a + b\n"
    result = DockerTestRunner(settings).run(files, phase="reproduce", attempt=0)
    assert result.outcome == "environment_error"
    assert "timeout" in result.output.lower()
    volumes = subprocess.run(["docker", "volume", "ls", "--filter", "label=patchpilot.temporary=true", "-q"], capture_output=True, text=True, check=True)
    assert not volumes.stdout.strip()


def test_real_docker_to_persistent_approval_and_mock_pr(sandbox_settings, tmp_path):
    from patchpilot.service import RepairService
    from patchpilot.verification import validate_repair

    files = fixture_files("assertion")
    calls, published = [], []
    class GitHub:
        def load_failure(self, repo, run_id):
            return FailureInfo(repository="controlled/demo", run_id="123", head_sha="a" * 40, log="AssertionError")

        def collect_context(self, failure):
            return files.copy()

        def create_draft_pr(self, state):
            validate_repair(state)
            assert state.approved
            published.append(state)
            return {"pr_url": "https://github.com/controlled/demo/pull/1", "published_sha": "b" * 40}

    class Runner(DockerTestRunner):
        def run(self, files, *, phase, attempt):
            calls.append((phase, attempt))
            return super().run(files, phase=phase, attempt=attempt)

    settings = sandbox_settings.model_copy(update={"data_dir": tmp_path})
    factory = lambda: (GitHub(), FakeLLMClient(), Runner(settings))
    first = RepairService(settings, adapter_factory=factory)
    job_id, _ = first.store.enqueue("controlled/demo", "123", head_sha="a" * 40)
    waiting = first.execute(job_id)
    assert waiting["status"] == "AWAITING_APPROVAL", waiting
    assert calls == [("reproduce", 0), ("repair", 1)] and not published
    resumed = RepairService(settings, adapter_factory=factory)
    approved = resumed.execute(job_id, resume=True)
    assert approved["status"] == "CI_PENDING" and len(published) == 1
    assert calls == [("reproduce", 0), ("repair", 1)]
