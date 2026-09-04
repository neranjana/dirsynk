"""Safety checks that run before any plan is built.

Every one of these returns a specific message rather than a boolean, because the point is
to tell the user exactly which rule stopped them.
"""

from __future__ import annotations

import os
from pathlib import Path

from .models import RESERVED_DIRS, JobConfig


def path_problem(raw: str, label: str) -> str | None:
    """Why this one path cannot be used, or None if it is fine.

    Used by the job editor to show a path in red before anything else is attempted.
    """
    if not raw.strip():
        return f"{label}: no folder chosen."
    path = Path(raw).expanduser()
    if not path.exists():
        return f"{label}: path not found — {path}"
    if not path.is_dir():
        return f"{label}: not a folder — {path}"
    if not os.access(path, os.R_OK | os.X_OK):
        return f"{label}: not readable — {path}"
    if any(part in RESERVED_DIRS for part in path.parts):
        return f"{label}: lies inside a {' or '.join(RESERVED_DIRS)} folder — {path}"
    return None


def _same_directory(a: Path, b: Path) -> bool:
    try:
        return a.samefile(b)  # authoritative, and case-correct on case-insensitive volumes
    except OSError:
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _is_inside(inner: Path, outer: Path) -> bool:
    inner_parts = [os.path.normcase(part) for part in inner.parts]
    outer_parts = [os.path.normcase(part) for part in outer.parts]
    return len(inner_parts) > len(outer_parts) and inner_parts[: len(outer_parts)] == outer_parts


def validate(config: JobConfig) -> list[str]:
    """Everything wrong with this job, in the order a user would want to fix it."""
    problems: list[str] = []
    for raw, label in ((config.path_a, "Folder A"), (config.path_b, "Folder B")):
        problem = path_problem(raw, label)
        if problem:
            problems.append(problem)
    if problems:
        return problems

    root_a = config.root_a.resolve()
    root_b = config.root_b.resolve()

    if _same_directory(root_a, root_b):
        return ["Folder A and Folder B are the same folder."]
    if _is_inside(root_a, root_b):
        problems.append(f"Folder A is inside Folder B ({root_a} inside {root_b}).")
    elif _is_inside(root_b, root_a):
        problems.append(f"Folder B is inside Folder A ({root_b} inside {root_a}).")

    # Whatever a run may write to has to be writable before we plan it.
    destinations = [(root_b, "Folder B")]
    if config.mode == "two_way":
        destinations.append((root_a, "Folder A"))
    for path, label in destinations:
        if not os.access(path, os.W_OK):
            problems.append(f"{label} is not writable — {path}")
    return problems
