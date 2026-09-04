"""Walk a directory tree into ``{relative path: Entry}``.

Directories are first-class entries, not implied parents, so an empty directory is
something the planner can see and act on. Errors are recorded per path and never raised:
one unreadable subdirectory must not cost you the rest of the scan.
"""

from __future__ import annotations

import fnmatch
import os
import stat
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import RESERVED_DIRS, Entry

#: How often the progress callback is invoked, in entries.
PROGRESS_EVERY = 256


@dataclass
class ScanResult:
    entries: dict[PurePosixPath, Entry] = field(default_factory=dict)
    #: Paths that could not be read, as human-readable messages.
    errors: list[str] = field(default_factory=list)
    #: Paths deliberately passed over (special files, symlink cycles).
    skipped: list[str] = field(default_factory=list)
    cancelled: bool = False

    def __len__(self) -> int:
        return len(self.entries)


@dataclass(frozen=True)
class ExcludeRules:
    """Compiled exclusion globs: ``dir/`` patterns apply to directories only."""

    any_kind: tuple[str, ...] = ()
    dirs_only: tuple[str, ...] = ()

    def matches(self, rel: PurePosixPath, is_dir: bool) -> bool:
        rel_str = str(rel)
        name = rel.name
        for pattern in self.any_kind:
            if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(name, pattern):
                return True
        if is_dir:
            for pattern in self.dirs_only:
                if fnmatch.fnmatch(rel_str, pattern) or fnmatch.fnmatch(name, pattern):
                    return True
        return False


def compile_excludes(patterns: Iterable[str]) -> ExcludeRules:
    """Split raw patterns into the two buckets the scanner matches against.

    A pattern is matched against the whole relative path *and* against the item's own
    name, so ``.DS_Store`` catches ``photos/.DS_Store`` as anyone would expect. A trailing
    slash (``__pycache__/``) means "this directory and everything under it".
    """
    any_kind: list[str] = []
    dirs_only: list[str] = []
    for raw in patterns:
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern.endswith("/"):
            dirs_only.append(pattern.rstrip("/"))
        else:
            any_kind.append(pattern)
    return ExcludeRules(tuple(any_kind), tuple(dirs_only))


def scan(
    root: Path,
    *,
    exclude: Sequence[str] = (),
    follow_symlinks: bool = False,
    cancel: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> ScanResult:
    """Walk ``root`` and return every file, directory and symlink beneath it.

    ``.deleted`` and ``.dirsynk`` are pruned at any depth, always, on both sides.
    """
    rules = compile_excludes(exclude)
    result = ScanResult()
    stack: list[tuple[Path, PurePosixPath]] = [(root, PurePosixPath("."))]
    seen_dirs: set[tuple[int, int]] = set()
    try:
        root_stat = root.stat()
        seen_dirs.add((root_stat.st_dev, root_stat.st_ino))
    except OSError as exc:
        result.errors.append(f"{root}: {exc.strerror or exc}")
        return result
    count = 0

    while stack:
        directory, dir_rel = stack.pop()
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            break
        try:
            with os.scandir(directory) as it:
                children = list(it)
        except OSError as exc:
            result.errors.append(f"{directory}: {exc.strerror or exc}")
            continue

        for child in children:
            if child.name in RESERVED_DIRS:
                continue
            rel = (
                PurePosixPath(child.name) if dir_rel == PurePosixPath(".") else dir_rel / child.name
            )
            try:
                is_link = child.is_symlink()
                lstat_result = child.stat(follow_symlinks=False)
            except OSError as exc:
                result.errors.append(f"{rel}: {exc.strerror or exc}")
                continue

            if is_link and not follow_symlinks:
                if rules.matches(rel, is_dir=False):
                    continue
                try:
                    target = os.readlink(child.path)
                except OSError as exc:
                    result.errors.append(f"{rel}: {exc.strerror or exc}")
                    continue
                result.entries[rel] = Entry(
                    rel=rel,
                    kind="symlink",
                    size=lstat_result.st_size,
                    mtime_ns=lstat_result.st_mtime_ns,
                    target=target,
                )
                count += 1
            else:
                try:
                    st = child.stat(follow_symlinks=follow_symlinks)
                except OSError as exc:
                    result.errors.append(f"{rel}: {exc.strerror or exc}")
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                if rules.matches(rel, is_dir=is_dir):
                    continue
                if is_dir:
                    key = (st.st_dev, st.st_ino)
                    if key in seen_dirs:
                        result.skipped.append(f"{rel}: symlink cycle, not followed")
                        continue
                    seen_dirs.add(key)
                    result.entries[rel] = Entry(
                        rel=rel, kind="dir", size=0, mtime_ns=st.st_mtime_ns
                    )
                    count += 1
                    stack.append((Path(child.path), rel))
                elif stat.S_ISREG(st.st_mode):
                    result.entries[rel] = Entry(
                        rel=rel, kind="file", size=st.st_size, mtime_ns=st.st_mtime_ns
                    )
                    count += 1
                else:
                    result.skipped.append(f"{rel}: special file, skipped")

            if progress is not None and count % PROGRESS_EVERY == 0:
                progress(count)

    if progress is not None:
        progress(count)
    return result
