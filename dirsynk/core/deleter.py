"""The two deletion policies.

``permanent`` removes the item. ``quarantine`` moves it into a ``.deleted`` directory at
the root it is being removed from, under a per-run timestamp folder, keeping its relative
path — so each of the two roots grows its own ``.deleted``. Nothing here ever purges
``.deleted``; that is deliberate, and the README says so.
"""

from __future__ import annotations

import errno
import os
import shutil
from datetime import datetime
from pathlib import Path, PurePosixPath

from .models import DeletionPolicy

QUARANTINE_DIR = ".deleted"


def run_stamp(now: datetime | None = None) -> str:
    """The per-run folder name inside ``.deleted``: ``YYYYMMDD-HHMMSS``."""
    return (now or datetime.now()).strftime("%Y%m%d-%H%M%S")


def quarantine_root(root: Path, stamp: str) -> Path:
    return root / QUARANTINE_DIR / stamp


def _free_name(target: Path) -> Path:
    """``photo.jpg`` → ``photo.jpg (2)`` → ``photo.jpg (3)``; never overwrite."""
    if not target.exists():
        return target
    counter = 2
    while True:
        candidate = target.with_name(f"{target.name} ({counter})")
        if not candidate.exists():
            return candidate
        counter += 1


def quarantine_target(root: Path, rel: PurePosixPath, stamp: str) -> Path:
    return quarantine_root(root, stamp).joinpath(*rel.parts)


def remove(
    path: Path,
    *,
    rel: PurePosixPath,
    root: Path,
    policy: DeletionPolicy,
    stamp: str,
    is_dir: bool,
) -> Path | None:
    """Delete one item under the job's policy. Returns where it went, if anywhere.

    Directories are expected to be empty by the time they get here: the executor removes
    files first and directories deepest-first.
    """
    if policy == "permanent":
        if is_dir:
            os.rmdir(path)
        else:
            os.remove(path)
        return None

    target = quarantine_target(root, rel, stamp)
    target.parent.mkdir(parents=True, exist_ok=True)

    if is_dir:
        # Its contents, if any, were quarantined under this same path already, so the
        # target directory may exist: keep it and drop the now-empty original.
        if target.exists() and not target.is_dir():
            target = _free_name(target)
        target.mkdir(parents=True, exist_ok=True)
        os.rmdir(path)
        return target

    target = _free_name(target)
    try:
        os.replace(path, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        # Different filesystem: copy across, then remove the original.
        shutil.copy2(path, target, follow_symlinks=False)
        os.remove(path)
    return target


def remove_tree(
    path: Path,
    *,
    rel: PurePosixPath,
    root: Path,
    policy: DeletionPolicy,
    stamp: str,
) -> Path | None:
    """Remove an item and everything under it, under the job's policy.

    Only needed to clear the way for a conflict resolution, where one side has a
    directory and the other a file of the same name.
    """
    is_dir = path.is_dir() and not path.is_symlink()
    if policy == "permanent":
        if is_dir:
            shutil.rmtree(path)
        else:
            os.remove(path)
        return None

    target = quarantine_target(root, rel, stamp)
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _free_name(target)
    try:
        os.replace(path, target)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        if is_dir:
            shutil.copytree(path, target, symlinks=True)
            shutil.rmtree(path)
        else:
            shutil.copy2(path, target, follow_symlinks=False)
            os.remove(path)
    return target
