"""Nine repair steps. LLM proposals never decide routing or policy."""

from .adapters import GitHubClient, LLMClient, TestRunner
from .policy import apply_changes, check_scope, final_diff
from .state import MAX_ATTEMPTS, AppliedPatch, Diagnosis, FailureClassification, Patch, RepairState, TestResult
from .verification import validate_repair


class RepairNodes:
    def __init__(self, github: GitHubClient, llm: LLMClient, runner: TestRunner):
        self.github = github
        self.llm = llm
        self.runner = runner

    def collect_context(self, state: RepairState) -> dict:
        files = self.github.collect_context(state.failure)
        if not files or not all(isinstance(k, str) and isinstance(v, str) for k, v in files.items()):
            raise ValueError("Context must be a nonempty mapping of paths to source strings")
        return {"files": files.copy(), "original_files": files.copy()}

    def reproduce_failure(self, state: RepairState) -> dict:
        result = TestResult.model_validate(self.runner.run(state.files.copy(), phase="reproduce", attempt=0))
        return {
            "reproduction": result,
            "status": "not_reproduced" if result.outcome == "passed" else "pending",
        }

    def classify_failure(self, state: RepairState) -> dict:
        if state.reproduction is None:
            raise ValueError("Reproduction result is required")
        environment = state.reproduction.outcome == "environment_error"
        if not environment:
            classification = FailureClassification.model_validate(self.llm.classify_failure(state.model_copy(deep=True)))
            environment = classification.category != "code"
        return {
            "classification": "environment" if environment else "code",
            "status": "manual_action_required" if environment else "pending",
        }

    def diagnose(self, state: RepairState) -> dict:
        diagnosis = Diagnosis.model_validate(self.llm.diagnose(state.model_copy(deep=True)))
        return {
            "diagnoses": [*state.diagnoses, diagnosis],
            "status": "pending" if diagnosis.category == "code" else "manual_action_required",
        }

    def generate_patch(self, state: RepairState) -> dict:
        if state.attempt >= MAX_ATTEMPTS:
            raise ValueError("Maximum repair attempts reached")
        next_state = RepairState.model_validate({**state.model_dump(), "attempt": state.attempt + 1})
        patch = Patch.model_validate(self.llm.generate_patch(next_state))
        return {"attempt": next_state.attempt, "patch": patch}

    def apply_patch(self, state: RepairState) -> dict:
        if state.patch is None:
            raise ValueError("Patch is required")
        files = apply_changes(state.files, state.patch)
        check_scope(state.original_files, files)
        return {
            "files": files,
            "applied_patches": [*state.applied_patches, AppliedPatch(attempt=state.attempt, patch=state.patch)],
        }

    def run_tests(self, state: RepairState) -> dict:
        result = TestResult.model_validate(
            self.runner.run(state.files.copy(), phase="repair", attempt=state.attempt)
        )
        status = "pending"
        if result.outcome == "environment_error":
            status = "manual_action_required"
        elif result.outcome == "failed" and state.attempt >= MAX_ATTEMPTS:
            status = "failed"
        return {"test_results": [*state.test_results, result], "status": status}

    def validate_patch(self, state: RepairState) -> dict:
        validate_repair(state)
        return {
            "status": "success" if state.simulated else "local_verified",
            "diff": final_diff(state.original_files, state.files),
        }

    def report_result(self, state: RepairState) -> dict:
        paths = sorted({c.path for p in state.applied_patches for c in p.patch.changes})
        if state.simulated:
            report = (
                f"[MOCK ONLY] status={state.status}; attempts={state.attempt}/{MAX_ATTEMPTS}; "
                f"changed_files={paths}. Results are scripted; repository tests were not executed."
            )
        else:
            from .reporting import approval_summary

            report = approval_summary(state)
        if state.error:
            report += f" Error: {state.error}"
        return {"report": report}
