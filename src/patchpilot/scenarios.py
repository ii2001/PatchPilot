"""Repeatable demos: each invocation uses fresh fake adapters and state."""

from .adapters import FakeGitHubClient, FakeLLMClient, FakeTestRunner
from .graph import build_graph
from .state import FailureInfo, RepairState, TestResult

SCENARIOS = ("success", "retry_success", "max_failures", "environment_error", "not_reproduced")


def run_scenario(name: str) -> RepairState:
    failed = TestResult(outcome="failed", output="[MOCK] AssertionError: add(2, 3) != 5")
    passed = TestResult(outcome="passed", output="[MOCK] scripted pytest outcome: passed")
    environment = TestResult(outcome="environment_error", output="[MOCK] Python interpreter unavailable")
    scenarios = {
        "success": (failed, (passed,), 1),
        "retry_success": (failed, (failed, passed), 2),
        "max_failures": (failed, (failed, failed, failed), None),
        "environment_error": (environment, (), None),
        "not_reproduced": (passed, (), None),
    }
    if name not in scenarios:
        raise ValueError(f"Unknown scenario: {name}")
    reproduction, repairs, correct_on_attempt = scenarios[name]
    graph = build_graph(
        FakeGitHubClient(), FakeLLMClient(correct_on_attempt), FakeTestRunner(reproduction, repairs)
    )
    state = RepairState(failure=FailureInfo(repository="demo/calculator", run_id="mock-1", log=failed.output))
    return RepairState.model_validate(graph.invoke(state.model_dump(), {"recursion_limit": 40}))
