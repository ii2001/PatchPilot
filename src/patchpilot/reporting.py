"""Structured metrics and human-readable approval evidence."""

from .policy import final_diff
from .state import RepairState

STATUS_LABELS = {
    "pending": "RUNNING", "failed": "REPAIR_FAILED", "not_reproduced": "NOT_REPRODUCED",
    "manual_action_required": "MANUAL_ACTION_REQUIRED", "local_verified": "LOCAL_VERIFIED",
    "approved": "APPROVED", "rejected": "REJECTED", "ci_pending": "CI_PENDING",
    "publish_failed": "PUBLISH_FAILED",
    "ci_verified": "CI_VERIFIED", "ci_failed": "CI_FAILED", "success": "MOCK_SUCCESS",
}


def approval_summary(state: RepairState) -> str:
    cause = state.diagnoses[-1].cause if state.diagnoses else (
        state.llm_calls[-1].response.get("reason", "No diagnosis available") if state.llm_calls
        else state.reproduction.output[:1200] if state.reproduction else "No diagnosis available"
    )
    files = sorted(path for path in state.files if state.files.get(path) != state.original_files.get(path))
    executions = state.test_results[-1].executions if state.test_results else []
    tests = "; ".join(
        f"{item.stage}: {' '.join(item.command)}; exit={item.exit_code}; "
        f"passed={item.passed}, collected={item.collected}, skipped={item.skipped}" for item in executions
    ) or "No successful repository test execution recorded"
    return (
        f"Status: {STATUS_LABELS[state.status]}\nCause: {cause}\n"
        f"Base commit: {state.failure.head_sha}\nFiles: {', '.join(files)}\n"
        f"Attempts: {state.attempt}/3\nTests: {tests}\n"
        "Verification: Docker local tests only; remote CI is reported separately.\n"
        "Risks: tests do not prove correctness; static bypass detection is conservative, not exhaustive; "
        "Docker is not a complete malicious-code isolation boundary. Human review is required."
    )


def result_payload(state: RepairState, *, awaiting_approval=False) -> dict:
    paths = sorted(path for path in state.files if state.files.get(path) != state.original_files.get(path))
    return {
        "status": "AWAITING_APPROVAL" if awaiting_approval else STATUS_LABELS[state.status],
        "simulated": state.simulated,
        "verification_level": "MOCK" if state.simulated else ("LOCAL_DOCKER" if state.diff else "UNVERIFIED"),
        "remote_ci_status": state.status.upper() if state.status.startswith("ci_") else "NOT_CHECKED",
        "metrics": {
            "active_seconds": state.elapsed_seconds,
            "llm_calls": len(state.llm_calls),
            "cached_llm_calls": sum(call.cached for call in state.llm_calls),
            "input_tokens": sum(call.input_tokens for call in state.llm_calls),
            "output_tokens": sum(call.output_tokens for call in state.llm_calls),
            "patch_attempts": state.attempt, "changed_files": len(paths),
            "local_tests_passed": bool(not state.simulated and state.test_results and state.test_results[-1].outcome == "passed"),
            "pr_url": state.pr_url,
        },
        "changed_files": paths, "diff": state.diff or final_diff(state.original_files, state.files),
        "summary": state.report or approval_summary(state), "state": state.model_dump(mode="json"),
    }
