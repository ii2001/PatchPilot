import pytest

from patchpilot.policy import apply_changes, check_path
from patchpilot.state import FileChange, Patch


@pytest.mark.parametrize("path", [
    "tests/test_app.py", ".github/workflows/ci.yml", ".git/config", "pkg/tests/test.py",
    "Tests/test.py", "../app.py", "src/../../app.py", "/tmp/app.py", "C:/app.py",
    "C:app.py", "src\\app.py", "src//app.py", "./app.py", "src/./app.py", "app.py/",
    "app\x00.py", "", ".", "..",
])
def test_rejects_protected_or_ambiguous_paths(path):
    with pytest.raises(ValueError):
        check_path(path)


@pytest.mark.parametrize("path", ["src/app.py", "app.py", "src/tests_helper.py", "README.md"])
def test_allows_regular_paths(path):
    check_path(path)


def test_apply_returns_new_workspace_without_mutation():
    files = {"src/app.py": "before", "tests/test_app.py": "unchanged"}
    patch = Patch(changes=[FileChange(path="src/app.py", before="before", after="after")])
    updated = apply_changes(files, patch)
    assert updated == {"src/app.py": "after", "tests/test_app.py": "unchanged"}
    assert files["src/app.py"] == "before"


def test_multi_file_patch_is_atomic():
    files = {"src/app.py": "before", "tests/test_app.py": "test"}
    patch = Patch(changes=[
        FileChange(path="src/app.py", before="before", after="after"),
        FileChange(path="tests/test_app.py", before="test", after="tampered"),
    ])
    with pytest.raises(ValueError, match="Protected"):
        apply_changes(files, patch)
    assert files == {"src/app.py": "before", "tests/test_app.py": "test"}


@pytest.mark.parametrize("path,before,after", [
    ("missing.py", "", "new"), ("src/app.py", "stale", "new"), ("src/app.py", "old", "old"),
])
def test_rejects_missing_stale_or_noop_changes(path, before, after):
    with pytest.raises(ValueError):
        apply_changes({"src/app.py": "old"}, Patch(changes=[FileChange(path=path, before=before, after=after)]))


def test_rejects_duplicate_changes():
    change = FileChange(path="src/app.py", before="old", after="new")
    with pytest.raises(ValueError, match="Duplicate"):
        apply_changes({"src/app.py": "old"}, Patch(changes=[change, change]))
