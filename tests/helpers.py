"""Helpers for building little trees on disk inside ``tmp_path``."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path


def write(path: Path, content: str = "", *, mtime_ns: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime_ns is not None:
        os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def build(root: Path, spec: Mapping[str, str | None]) -> Path:
    """Create a tree. A ``None`` value means "an empty directory"."""
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in spec.items():
        target = root / rel
        if content is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            write(target, content)
    return root


def set_mtime(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


def mtime_ns(path: Path) -> int:
    return path.stat().st_mtime_ns
