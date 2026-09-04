from __future__ import annotations

import errno
import os
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

import pytest
from helpers import build

from dirsynk.core import deleter

STAMP = "20260904-120000"


def rm(root: Path, rel: str, *, policy: str = "quarantine", is_dir: bool = False) -> Path | None:
    return deleter.remove(
        root.joinpath(*PurePosixPath(rel).parts),
        rel=PurePosixPath(rel),
        root=root,
        policy=policy,  # type: ignore[arg-type]
        stamp=STAMP,
        is_dir=is_dir,
    )


def test_run_stamp_is_a_sortable_timestamp() -> None:
    assert deleter.run_stamp(datetime(2026, 9, 4, 12, 0, 0)) == "20260904-120000"


def test_permanent_removes_a_file(tmp_path: Path) -> None:
    root = build(tmp_path, {"f.txt": "x"})
    assert rm(root, "f.txt", policy="permanent") is None
    assert not (root / "f.txt").exists()


def test_permanent_removes_an_empty_directory(tmp_path: Path) -> None:
    root = build(tmp_path, {"d": None})
    rm(root, "d", policy="permanent", is_dir=True)
    assert not (root / "d").exists()


def test_permanent_refuses_a_non_empty_directory(tmp_path: Path) -> None:
    root = build(tmp_path, {"d/inside.txt": "x"})
    with pytest.raises(OSError):
        rm(root, "d", policy="permanent", is_dir=True)


def test_quarantine_keeps_the_relative_path_under_a_run_folder(tmp_path: Path) -> None:
    root = build(tmp_path, {"photos/2024/pic.jpg": "bytes"})
    target = rm(root, "photos/2024/pic.jpg")
    assert target == root / ".deleted" / STAMP / "photos" / "2024" / "pic.jpg"
    assert target.read_text() == "bytes"
    assert not (root / "photos" / "2024" / "pic.jpg").exists()


def test_quarantine_never_overwrites_an_existing_name(tmp_path: Path) -> None:
    root = build(tmp_path, {"f.txt": "first"})
    first = rm(root, "f.txt")
    build(root, {"f.txt": "second"})
    second = rm(root, "f.txt")
    build(root, {"f.txt": "third"})
    third = rm(root, "f.txt")
    assert first.name == "f.txt"
    assert second.name == "f.txt (2)"
    assert third.name == "f.txt (3)"
    assert [p.read_text() for p in (first, second, third)] == ["first", "second", "third"]


def test_quarantine_of_nested_empty_dirs_reproduces_the_structure(tmp_path: Path) -> None:
    root = build(tmp_path, {"outer/inner": None})
    rm(root, "outer/inner", is_dir=True)
    rm(root, "outer", is_dir=True)
    assert (root / ".deleted" / STAMP / "outer" / "inner").is_dir()
    assert not (root / "outer").exists()


def test_quarantine_of_a_directory_keeps_the_files_already_moved_into_it(tmp_path: Path) -> None:
    root = build(tmp_path, {"d/f.txt": "x"})
    rm(root, "d/f.txt")
    rm(root, "d", is_dir=True)
    assert (root / ".deleted" / STAMP / "d" / "f.txt").read_text() == "x"
    assert not (root / "d").exists()


def test_quarantine_falls_back_to_copy_when_the_move_crosses_devices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build(tmp_path, {"f.txt": "payload"})
    real_replace = os.replace

    def cross_device(src, dst, **kwargs):
        raise OSError(errno.EXDEV, "Cross-device link")

    monkeypatch.setattr(deleter.os, "replace", cross_device)
    target = rm(root, "f.txt")
    monkeypatch.setattr(deleter.os, "replace", real_replace)
    assert target.read_text() == "payload"
    assert not (root / "f.txt").exists()


def test_a_move_failure_that_is_not_cross_device_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = build(tmp_path, {"f.txt": "payload"})
    monkeypatch.setattr(
        deleter.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "nope"))
    )
    with pytest.raises(OSError):
        rm(root, "f.txt")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks")
def test_quarantine_moves_a_symlink_without_following_it(tmp_path: Path) -> None:
    root = build(tmp_path, {"real.txt": "data"})
    (root / "link.txt").symlink_to(root / "real.txt")
    target = rm(root, "link.txt")
    assert target.is_symlink()
    assert (root / "real.txt").read_text() == "data"
