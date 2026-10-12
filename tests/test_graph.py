import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from patchpilot.adapters import FakeGitHubClient, FakeLLMClient, FakeTestRunner, SOURCE_PATH
from patchpilot.graph import after_tests, build_graph
from patchpilot.nodes import RepairNodes
from patchpilot.scenarios import SCENARIOS, run_scenario
from patchpilot.state import FailureInfo, FileChange, Patch, RepairState, TestResult as RunnerResult


@pytest.fixture
def initial_state():
    return RepairState(failure=FailureInfo(repository="demo/repo", run_id="mock", log="AssertionError"))


@pytest.mark.parametrize(
    "scenario,status,attempts",
    [
        ("success", "success", 1),
        ("retry_success", "success", 2),
        ("max_failures", "failed", 3),
        ("environment_error", "manual_action_required", 0),
        ("not_reproduced", "not_reproduced", 0),
    ],
)
def test_scenario_branches(scenario, status, attempts):
    state = run_scenario(scenario)
    assert state.status == status
    assert state.attempt == attempts
    assert len(state.applied_patches) == len(state.test_results) == attempts
    assert len(state.diagnoses) == attempts
    assert state.trace[:2] == ["collect_context", "reproduce_failure"]
    assert state.trace[-1] == "report_result"
    assert state.trace.count("diagnose") == attempts
    assert state.trace.count("validate_patch") == (1 if status == "success" else 0)
    assert ("classify_failure" in state.trace) == (scenario != "not_reproduced")
    assert all(result.simulated for result in state.test_results)
    assert "MOCK ONLY" in state.report and "not executed" in state.report
    assert state.files["tests/test_calculator.py"] == state.original_files["tests/test_calculator.py"]
    assert state.files[".github/workflows/ci.yml"] == state.original_files[".github/workflows/ci.yml"]


def test_success_node_order():
    assert run_scenario("success").trace == [
        "collect_context", "reproduce_failure", "classify_failure", "diagnose",
        "generate_patch", "apply_patch", "run_tests", "validate_patch", "report_result",
    ]


def test_retry_diagnosis_receives_latest_result():
    state = run_scenario("retry_success")
    assert state.test_results[0].output in state.diagnoses[1].cause
    assert state.applied_patches[1].patch.changes[0].before == state.applied_patches[0].patch.changes[0].after


def test_repeat_invocations_are_isolated():
    assert run_scenario("retry_success") == run_scenario("retry_success")


def test_environment_error_during_repair(initial_state):
    failed = RunnerResult(outcome="failed", output="[MOCK] assertion")
    environment = RunnerResult(outcome="environment_error", output="[MOCK] dependency unavailable")
    graph = build_graph(FakeGitHubClient(), FakeLLMClient(), FakeTestRunner(failed, (environment,)))
    state = RepairState.model_validate(graph.invoke(initial_state.model_dump()))
    assert state.status == "manual_action_required"
    assert state.attempt == 1
    assert "validate_patch" not in state.trace


@pytest.mark.parametrize("path", ["tests/test_calculator.py", ".github/workflows/ci.yml", "../escape.py"])
def test_unsafe_llm_patch_terminates_without_applying(initial_state, path):
    class UnsafeLLM(FakeLLMClient):
        def generate_patch(self, state):
            return Patch(changes=[FileChange(path=path, before=state.files.get(path, ""), after="unsafe")])

    failed = RunnerResult(outcome="failed", output="[MOCK] assertion")
    graph = build_graph(FakeGitHubClient(), UnsafeLLM(), FakeTestRunner(failed, ()))
    state = RepairState.model_validate(graph.invoke(initial_state.model_dump()))
    assert state.status == "failed"
    assert state.files == state.original_files
    assert not state.applied_patches
    assert not state.test_results
    assert state.trace[-2:] == ["apply_patch", "report_result"]
    assert "ValueError" in state.error


@pytest.mark.parametrize("broken_adapter", ["github", "diagnose", "generate_patch", "runner"])
def test_adapter_errors_are_reported(initial_state, broken_adapter):
    class BrokenGitHub(FakeGitHubClient):
        def collect_context(self, failure):
            raise RuntimeError("fake adapter unavailable")

    class BrokenLLM(FakeLLMClient):
        def diagnose(self, state):
            if broken_adapter == "diagnose":
                raise RuntimeError("fake adapter unavailable")
            return super().diagnose(state)

        def generate_patch(self, state):
            raise RuntimeError("fake adapter unavailable")

    class BrokenRunner(FakeTestRunner):
        def run(self, files, *, phase, attempt):
            raise RuntimeError("fake adapter unavailable")

    failed = RunnerResult(outcome="failed", output="[MOCK] assertion")
    github = BrokenGitHub() if broken_adapter == "github" else FakeGitHubClient()
    llm = BrokenLLM() if broken_adapter in {"diagnose", "generate_patch"} else FakeLLMClient()
    runner = BrokenRunner(failed, ()) if broken_adapter == "runner" else FakeTestRunner(failed, ())
    state = RepairState.model_validate(build_graph(github, llm, runner).invoke(initial_state.model_dump()))
    assert state.status == "failed"
    assert "RuntimeError" in state.error
    assert state.trace[-1] == "report_result"


def test_validation_rejects_unrecorded_workspace_change():
    state = run_scenario("success")
    tampered = state.model_copy(update={"files": {**state.files, SOURCE_PATH: "unrecorded edit"}})
    nodes = RepairNodes(FakeGitHubClient(), FakeLLMClient(), FakeTestRunner(state.reproduction, ()))
    with pytest.raises(ValueError, match="history"):
        nodes.validate_patch(tampered)


def test_validation_requires_passing_result_and_patch(initial_state):
    failed = RunnerResult(outcome="failed", output="[MOCK] assertion")
    nodes = RepairNodes(FakeGitHubClient(), FakeLLMClient(), FakeTestRunner(failed, ()))
    with pytest.raises(ValueError, match="passing"):
        nodes.validate_patch(initial_state)
    with pytest.raises(ValueError, match="No applied patch"):
        nodes.validate_patch(initial_state.model_copy(update={"test_results": [RunnerResult(outcome="passed", output="mock")]}))


def test_attempt_cap_and_mock_marker_are_validated(initial_state):
    with pytest.raises(ValidationError):
        RepairState.model_validate({**initial_state.model_dump(), "attempt": 4})
    with pytest.raises(ValidationError):
        RunnerResult(outcome="passed", output="pretended real result", simulated=False)
    nodes = RepairNodes(FakeGitHubClient(), FakeLLMClient(), FakeTestRunner(RunnerResult(outcome="failed", output="mock"), ()))
    with pytest.raises(ValueError, match="Maximum"):
        nodes.generate_patch(initial_state.model_copy(update={"attempt": 3}))


@pytest.mark.parametrize("attempt,next_node", [(1, "diagnose"), (2, "diagnose"), (3, "report_result")])
def test_retry_boundary(initial_state, attempt, next_node):
    state = initial_state.model_copy(update={"attempt": attempt, "test_results": [RunnerResult(outcome="failed", output="mock")]})
    assert after_tests(state) == next_node


def test_cli_demo_all():
    result = subprocess.run([sys.executable, "-m", "patchpilot", "demo"], capture_output=True, text=True, check=True)
    reports = [json.loads(line) for line in result.stdout.splitlines()]
    assert [report["scenario"] for report in reports] == list(SCENARIOS)
    assert all(report["simulated"] for report in reports)
    assert [report["attempt"] for report in reports] == [1, 2, 3, 0, 0]


def test_cli_single_scenario():
    result = subprocess.run(
        [sys.executable, "-m", "patchpilot", "demo", "--scenario", "retry_success"],
        capture_output=True, text=True, check=True,
    )
    assert json.loads(result.stdout)["status"] == "success"


def test_cli_rejects_unknown_scenario():
    result = subprocess.run([sys.executable, "-m", "patchpilot", "demo", "--scenario", "unknown"], capture_output=True, text=True)
    assert result.returncode == 2


def test_unknown_scenario_rejected():
    with pytest.raises(ValueError, match="Unknown scenario"):
        run_scenario("unknown")
