"""Deterministic, atomic patch policy for the in-memory workspace."""

import ast
import difflib
import hashlib
import json
from collections import Counter
from pathlib import PurePosixPath, PureWindowsPath

from .state import Patch


def check_relative_path(path: str) -> None:
    parsed = PurePosixPath(path)
    if (
        not path
        or "\x00" in path
        or "\\" in path
        or parsed.is_absolute()
        or PureWindowsPath(path).drive
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise ValueError(f"Unsafe repository-relative path: {path!r}")


def check_path(path: str) -> None:
    check_relative_path(path)
    parsed = PurePosixPath(path)
    if any(part.casefold() in {"tests", ".github", ".git"} for part in parsed.parts):
        raise ValueError(f"Protected path: {path!r}")
    name = parsed.name.casefold()
    if name.startswith("test_") or name.endswith("_test.py") or name in {
        "conftest.py", "pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "setup.py",
        "sitecustomize.py", "usercustomize.py", "pytest.py", "unittest.py",
    }:
        raise ValueError(f"Protected test/configuration path: {path!r}")


def workspace_digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def final_diff(original: dict[str, str], updated: dict[str, str]) -> str:
    lines = []
    for path in sorted(original):
        if original[path] != updated[path]:
            for line in difflib.unified_diff(
                original[path].splitlines(keepends=True), updated[path].splitlines(keepends=True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            ):
                lines.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(lines)


def check_scope(original: dict[str, str], updated: dict[str, str]) -> None:
    if original.keys() != updated.keys():
        raise ValueError("File additions/deletions are not supported")
    changed = [path for path in original if original[path] != updated[path]]
    if len(changed) > 3:
        raise ValueError("Patch exceeds the three-file limit")
    lines = 0
    for path in changed:
        check_path(path)
        if not path.endswith(".py"):
            raise ValueError("Only existing Python source files may be repaired")
        for operation, a, b, c, d in difflib.SequenceMatcher(
            a=original[path].splitlines(), b=updated[path].splitlines(), autojunk=False
        ).get_opcodes():
            if operation != "equal":
                lines += (b - a) + (d - c)
    if lines > 100:
        raise ValueError("Patch exceeds 100 added/deleted lines")


def _bypass_operations(source: str) -> Counter:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return Counter()
    operations = Counter()
    for node in ast.walk(tree):
        text = ast.unparse(node)
        if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
            word in text for word in ("pytest", "unittest", "_exit", " exit")
        ):
            operations[text] += 1
        elif isinstance(node, ast.Call) and (
            any(word in text for word in (
                "pytest", "unittest", "._exit(", ".exit(", "sys.modules", "PYTEST_",
                "settrace(", "import_module(", "__import__(",
            )) or isinstance(node.func, ast.Name) and node.func.id in {"exec", "eval", "exit", "quit"}
        ):
            operations[text] += 1
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)) and any(
            word in text for word in ("pytest", "sys.modules", "PYTEST_")
        ):
            operations[text] += 1
    return operations


def check_code(before: str, after: str) -> None:
    try:
        parsed = ast.parse(after)
    except SyntaxError as exc:
        raise ValueError("Patch introduces invalid Python syntax") from exc
    if _bypass_operations(after) - _bypass_operations(before):
        raise ValueError("Patch introduces a test/validation bypass operation")
    try:
        original = ast.parse(before)
    except SyntaxError:
        original = ast.Module(body=[], type_ignores=[])
    if sum(isinstance(n, ast.Assert) for n in ast.walk(parsed)) < sum(
        isinstance(n, ast.Assert) for n in ast.walk(original)
    ):
        raise ValueError("Patch removes assertions")


def apply_changes(files: dict[str, str], patch: Patch) -> dict[str, str]:
    seen: set[str] = set()
    for change in patch.changes:
        check_path(change.path)
        if change.path in seen:
            raise ValueError(f"Duplicate patch path: {change.path!r}")
        seen.add(change.path)
        if change.path not in files or files[change.path] != change.before:
            raise ValueError(f"Patch does not match existing content: {change.path!r}")
        if change.before == change.after:
            raise ValueError(f"Patch has no change: {change.path!r}")
        check_code(change.before, change.after)
    updated = files.copy()
    updated.update({change.path: change.after for change in patch.changes})
    check_scope(files, updated)
    return updated
