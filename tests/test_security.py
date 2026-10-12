import pytest

from patchpilot.policy import apply_changes, check_scope, final_diff, workspace_digest
from patchpilot.state import FileChange, Patch
from patchpilot.verification import validate_repair
from test_real_adapters import verified_state


@pytest.mark.parametrize("after", [
    "import pytest\npytest.skip('bypass')\n", "import os\nos._exit(0)\n",
    "import sys\nsys.exit(0)\n", "exec('skip checks')\n",
    "__import__('pytest').skip('bypass')\n", "import pytest as p\np.main([])\n",
    "import unittest\nunittest.skip('bypass')\n", "def f():\n    return True\n",
    "from os import _exit as stop\nstop(0)\n", "from sys import exit as stop\nstop(0)\n",
])
def test_rejects_test_bypass_or_assert_removal(after):
    before = "def f():\n    assert True\n    return 1\n"
    with pytest.raises(ValueError):
        apply_changes({"src/app.py": before}, Patch(changes=[FileChange(path="src/app.py", before=before, after=after)]))


def test_syntax_error_can_be_fixed():
    assert apply_changes({"app.py": "def f(:\n"}, Patch(changes=[
        FileChange(path="app.py", before="def f(:\n", after="def f():\n    return 1\n")
    ]))["app.py"].startswith("def f()")


@pytest.mark.parametrize("file_count,line_count", [(4, 1), (1, 101)])
def test_cumulative_patch_limits(file_count, line_count):
    original = {f"app{index}.py": "x = 0\n" for index in range(file_count)}
    updated = {path: "x = 1\n" * line_count for path in original}
    with pytest.raises(ValueError):
        check_scope(original, updated)


def test_diff_preserves_non_newline_terminated_changes():
    diff = final_diff({"app.py": "x = 0"}, {"app.py": "x = 1"})
    assert "-x = 0\n\\ No newline at end of file\n+x = 1\n" in diff


@pytest.mark.parametrize("case", ["mixed", "digest", "reproduction", "missing_suite", "exit", "removed", "skipped"])
def test_real_verification_rejects_forged_or_incomplete_evidence(case):
    state = verified_state()
    result = state.test_results[-1]
    full = result.executions[-1]
    if case == "mixed":
        result = result.model_copy(update={"simulated": True})
    elif case == "digest":
        result = result.model_copy(update={"workspace_digest": workspace_digest({"other.py": "x"})})
    elif case == "reproduction":
        state = state.model_copy(update={"reproduction": state.reproduction.model_copy(update={"workspace_digest": "0" * 64})})
    elif case == "missing_suite":
        result = result.model_copy(update={"executions": [result.executions[0]]})
    else:
        full = full.model_copy(update={"exit_code": 1} if case == "exit" else {"node_ids": []} if case == "removed" else {"skipped": 1})
        result = result.model_copy(update={"executions": [result.executions[0], full]})
    state = state.model_copy(update={"test_results": [result]})
    with pytest.raises(ValueError):
        validate_repair(state)
