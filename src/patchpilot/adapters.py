"""Small adapter contracts; fake implementations never execute repository code."""

from typing import Literal, Protocol

from .state import Diagnosis, FailureClassification, FailureInfo, FileChange, Patch, RepairState, TestResult

SOURCE_PATH = "src/calculator.py"
BROKEN_SOURCE = "def add(a, b):\n    return a - b\n"
FIXED_SOURCE = "def add(a, b):\n    return a + b\n"


class GitHubClient(Protocol):
    def collect_context(self, failure: FailureInfo) -> dict[str, str]: ...


class LLMClient(Protocol):
    def classify_failure(self, state: RepairState) -> FailureClassification: ...

    def diagnose(self, state: RepairState) -> Diagnosis: ...

    def generate_patch(self, state: RepairState) -> Patch: ...


class TestRunner(Protocol):
    def run(
        self, files: dict[str, str], *, phase: Literal["reproduce", "repair"], attempt: int
    ) -> TestResult: ...


class FakeGitHubClient:
    def collect_context(self, failure: FailureInfo) -> dict[str, str]:
        return {
            SOURCE_PATH: BROKEN_SOURCE,
            "tests/test_calculator.py": "def test_add():\n    assert add(2, 3) == 5\n",
            ".github/workflows/ci.yml": "name: demo-only\n",
        }


class FakeLLMClient:
    def __init__(self, correct_on_attempt: int | None = 1):
        self.correct_on_attempt = correct_on_attempt

    def classify_failure(self, state: RepairState) -> FailureClassification:
        return FailureClassification(category="code", reason="[FAKE LLM] scripted code failure")

    def diagnose(self, state: RepairState) -> Diagnosis:
        evidence = state.test_results[-1].output if state.test_results else state.failure.log
        return Diagnosis(
            cause=f"[FAKE LLM] add subtracts instead of adding. Evidence: {evidence}",
            suggested_fix="Replace subtraction with addition.",
        )

    def generate_patch(self, state: RepairState) -> Patch:
        correct = self.correct_on_attempt is not None and state.attempt >= self.correct_on_attempt
        after = FIXED_SOURCE if correct else f"def add(a, b):\n    return a - b + {state.attempt}\n"
        return Patch(changes=[FileChange(path=SOURCE_PATH, before=state.files[SOURCE_PATH], after=after)])


class FakeTestRunner:
    def __init__(self, reproduction: TestResult, repairs: tuple[TestResult, ...]):
        self.reproduction = reproduction
        self.repairs = repairs

    def run(
        self, files: dict[str, str], *, phase: Literal["reproduce", "repair"], attempt: int
    ) -> TestResult:
        if phase == "reproduce":
            return self.reproduction
        if not 1 <= attempt <= len(self.repairs):
            raise ValueError("Fake repair result is not configured for this attempt")
        return self.repairs[attempt - 1]
