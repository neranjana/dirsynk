from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from helpers import build

from dirsynk.core.models import JobConfig
from dirsynk.core.validate import path_problem, validate

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def config(a, b, **kwargs) -> JobConfig:
    return JobConfig(name="t", path_a=str(a), path_b=str(b), **kwargs)


def test_a_valid_pair_has_no_problems(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {})
    assert validate(config(a, b)) == []


def test_the_same_directory_is_refused(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    problems = validate(config(a, a))
    assert problems == ["Folder A and Folder B are the same folder."]


def test_a_different_spelling_of_the_same_directory_is_refused(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"sub": None})
    problems = validate(config(a, a / "sub" / ".."))
    assert problems == ["Folder A and Folder B are the same folder."]


def test_nesting_is_refused_in_both_directions(tmp_path: Path) -> None:
    outer = build(tmp_path / "outer", {"inner": None})
    inner = outer / "inner"
    assert "inside" in validate(config(inner, outer))[0]
    assert "inside" in validate(config(outer, inner))[0]


def test_a_missing_path_is_named(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    problems = validate(config(a, tmp_path / "nope"))
    assert problems == [f"Folder B: path not found — {tmp_path / 'nope'}"]


def test_a_file_is_not_a_folder(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "x"})
    assert "not a folder" in validate(config(a, a / "f.txt"))[0]


def test_an_empty_choice_is_named(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    assert validate(config(a, "")) == ["Folder B: no folder chosen."]


def test_paths_inside_reserved_folders_are_refused(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    quarantined = build(tmp_path / "b" / ".deleted" / "20260101-000000", {})
    assert "lies inside" in validate(config(a, quarantined))[0]


@POSIX_ONLY
def test_an_unwritable_destination_is_refused(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {})
    os.chmod(b, 0o555)
    try:
        problems = validate(config(a, b))
    finally:
        os.chmod(b, 0o755)
    assert "not writable" in problems[0]


@POSIX_ONLY
def test_two_way_needs_both_sides_writable(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {})
    os.chmod(a, 0o555)
    try:
        mirror_problems = validate(config(a, b))
        two_way_problems = validate(config(a, b, mode="two_way"))
    finally:
        os.chmod(a, 0o755)
    assert mirror_problems == []  # A is only ever read in mirror mode
    assert "Folder A is not writable" in two_way_problems[0]


@POSIX_ONLY
def test_an_unreadable_folder_is_refused(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    os.chmod(a, 0o000)
    try:
        problem = path_problem(str(a), "Folder A")
    finally:
        os.chmod(a, 0o755)
    assert problem is not None and "not readable" in problem
