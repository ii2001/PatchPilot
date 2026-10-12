"""Deterministic, atomic patch policy for the in-memory workspace."""

from pathlib import PurePosixPath, PureWindowsPath

from .state import Patch


def check_path(path: str) -> None:
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
    if any(part.casefold() in {"tests", ".github", ".git"} for part in parsed.parts):
        raise ValueError(f"Protected path: {path!r}")


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
    updated = files.copy()
    updated.update({change.path: change.after for change in patch.changes})
    return updated
