"""Independent deterministic verification, also rechecked before publishing."""

from .policy import apply_changes, check_scope, workspace_digest
from .state import RepairState


def validate_repair(state: RepairState) -> None:
    if not state.test_results or state.test_results[-1].outcome != "passed":
        raise ValueError("A passing result is required")
    if not state.applied_patches:
        raise ValueError("No applied patch to validate")
    expected = state.original_files.copy()
    for applied in state.applied_patches:
        expected = apply_changes(expected, applied.patch)
    if expected != state.files or state.files == state.original_files:
        raise ValueError("Workspace does not match the validated patch history")
    check_scope(state.original_files, state.files)
    if state.simulated:
        if any(not result.simulated for result in state.test_results):
            raise ValueError("Mock run has mixed result provenance")
        return
    if not state.failure.head_sha or not state.reproduction or state.reproduction.simulated:
        raise ValueError("Real runs require a pinned commit and real reproduction evidence")
    if state.reproduction.workspace_digest != workspace_digest(state.original_files):
        raise ValueError("Reproduction did not test the original workspace")
    result = state.test_results[-1]
    if result.simulated or result.workspace_digest != workspace_digest(state.files):
        raise ValueError("Passing result does not match the patched workspace")
    if len(result.executions) != 2 or {item.stage for item in result.executions} != {"targeted", "full"}:
        raise ValueError("Targeted and full-suite results are required")
    for execution in result.executions:
        if execution.exit_code != 0 or execution.collected < 1 or execution.passed < 1 or execution.failed:
            raise ValueError("Test execution evidence contradicts a passing result")
    baseline = next((item for item in state.reproduction.executions if item.stage == "full"), None)
    full = next(item for item in result.executions if item.stage == "full")
    if not baseline or not set(baseline.node_ids).issubset(full.node_ids) or full.skipped > baseline.skipped:
        raise ValueError("Tests were removed or newly skipped")
