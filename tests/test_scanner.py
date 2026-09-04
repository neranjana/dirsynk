from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath

import pytest
from helpers import build

from dirsynk.core.scanner import compile_excludes, scan

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions/symlinks")


def rels(result) -> set[str]:
    return {str(rel) for rel in result.entries}


def test_scans_nested_tree_with_files_and_dirs(tmp_path: Path) -> None:
    build(tmp_path, {"a.txt": "a", "sub/b.txt": "b", "sub/deep/c.txt": "c"})
    result = scan(tmp_path)
    assert rels(result) == {"a.txt", "sub", "sub/b.txt", "sub/deep", "sub/deep/c.txt"}
    assert result.entries[PurePosixPath("sub")].kind == "dir"
    assert result.entries[PurePosixPath("a.txt")].size == 1


def test_empty_directories_are_entries_in_their_own_right(tmp_path: Path) -> None:
    build(tmp_path, {"empty": None, "nested/also_empty": None})
    result = scan(tmp_path)
    assert rels(result) == {"empty", "nested", "nested/also_empty"}
    assert result.entries[PurePosixPath("empty")].kind == "dir"
    assert result.entries[PurePosixPath("empty")].size == 0


def test_relative_paths_are_posix_style_regardless_of_platform(tmp_path: Path) -> None:
    build(tmp_path, {"sub/deep/c.txt": "c"})
    result = scan(tmp_path)
    assert "sub/deep/c.txt" in rels(result)


def test_exclusion_matches_bare_name_at_any_depth(tmp_path: Path) -> None:
    build(tmp_path, {".DS_Store": "x", "photos/.DS_Store": "x", "photos/keep.jpg": "j"})
    result = scan(tmp_path, exclude=[".DS_Store"])
    assert rels(result) == {"photos", "photos/keep.jpg"}


def test_exclusion_glob_and_path_pattern(tmp_path: Path) -> None:
    build(tmp_path, {"a.tmp": "x", "logs/b.log": "x", "logs/c.txt": "x"})
    result = scan(tmp_path, exclude=["*.tmp", "logs/*.log"])
    assert rels(result) == {"logs", "logs/c.txt"}


def test_trailing_slash_pattern_prunes_the_whole_directory(tmp_path: Path) -> None:
    build(tmp_path, {"__pycache__/x.pyc": "x", "src/__pycache__/y.pyc": "y", "src/m.py": "m"})
    result = scan(tmp_path, exclude=["__pycache__/"])
    assert rels(result) == {"src", "src/m.py"}


def test_trailing_slash_pattern_does_not_match_a_file_of_that_name(tmp_path: Path) -> None:
    build(tmp_path, {"build": "not a dir"})
    result = scan(tmp_path, exclude=["build/"])
    assert rels(result) == {"build"}


def test_reserved_directories_are_always_skipped(tmp_path: Path) -> None:
    build(
        tmp_path, {".deleted/20260101-000000/old.txt": "o", ".dirsynk/x.json": "{}", "k.txt": "k"}
    )
    result = scan(tmp_path)
    assert rels(result) == {"k.txt"}


@POSIX_ONLY
def test_unreadable_subdirectory_is_recorded_and_the_walk_continues(tmp_path: Path) -> None:
    build(tmp_path, {"locked/secret.txt": "s", "open/fine.txt": "f"})
    locked = tmp_path / "locked"
    os.chmod(locked, 0o000)
    try:
        result = scan(tmp_path)
    finally:
        os.chmod(locked, 0o755)
    assert "open/fine.txt" in rels(result)
    assert "locked" in rels(result)
    assert any("locked" in message for message in result.errors)


@POSIX_ONLY
def test_symlink_is_recorded_as_its_own_kind_when_not_followed(tmp_path: Path) -> None:
    build(tmp_path, {"real.txt": "hello"})
    (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    result = scan(tmp_path, follow_symlinks=False)
    link = result.entries[PurePosixPath("link.txt")]
    assert link.kind == "symlink"
    assert link.target == str(tmp_path / "real.txt")


@POSIX_ONLY
def test_symlinked_directory_is_not_traversed_when_not_followed(tmp_path: Path) -> None:
    build(tmp_path, {"real/inside.txt": "i"})
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    result = scan(tmp_path, follow_symlinks=False)
    assert "link/inside.txt" not in rels(result)
    assert result.entries[PurePosixPath("link")].kind == "symlink"


@POSIX_ONLY
def test_following_symlinks_detects_cycles(tmp_path: Path) -> None:
    build(tmp_path, {"sub/inside.txt": "i"})
    (tmp_path / "sub" / "loop").symlink_to(tmp_path, target_is_directory=True)
    result = scan(tmp_path, follow_symlinks=True)
    assert "sub/inside.txt" in rels(result)
    assert any("cycle" in message for message in result.skipped)


@POSIX_ONLY
def test_special_files_are_skipped_not_treated_as_errors(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    build(tmp_path, {"a.txt": "a"})
    result = scan(tmp_path)
    assert rels(result) == {"a.txt"}
    assert result.errors == []
    assert any("special file" in message for message in result.skipped)


def test_progress_callback_reports_the_final_count(tmp_path: Path) -> None:
    build(tmp_path, {f"f{i}.txt": "x" for i in range(5)})
    seen: list[int] = []
    result = scan(tmp_path, progress=seen.append)
    assert seen[-1] == len(result.entries) == 5


def test_cancellation_stops_the_walk(tmp_path: Path) -> None:
    import threading

    build(tmp_path, {"sub/a.txt": "a"})
    cancel = threading.Event()
    cancel.set()
    result = scan(tmp_path, cancel=cancel)
    assert result.cancelled


def test_missing_root_is_an_error_not_an_exception(tmp_path: Path) -> None:
    result = scan(tmp_path / "nope")
    assert result.entries == {}
    assert result.errors


def test_compile_excludes_splits_dir_only_patterns() -> None:
    rules = compile_excludes(["*.tmp", "  ", "node_modules/"])
    assert rules.any_kind == ("*.tmp",)
    assert rules.dirs_only == ("node_modules",)
