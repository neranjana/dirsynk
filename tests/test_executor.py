from __future__ import annotations

import os
import shutil
import sys
import threading
from pathlib import Path, PurePosixPath

import pytest
from helpers import build

from dirsynk.core import executor
from dirsynk.core.executor import RunEvent, execute, ordered_actions
from dirsynk.core.models import TEMP_SUFFIX, Action, JobConfig, Plan
from dirsynk.core.planner import preview

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def config(a: Path, b: Path, **kwargs) -> JobConfig:
    return JobConfig(name="t", path_a=str(a), path_b=str(b), exclude=[], **kwargs)


def run(a: Path, b: Path, *, cancel=None, on_event=None, **kwargs):
    job = config(a, b, **kwargs)
    plan, _, _ = preview(job)
    return plan, execute(plan, job, cancel=cancel, on_event=on_event)


def action(kind: str, rel: str) -> Action:
    return Action(
        kind=kind,  # type: ignore[arg-type]
        rel=PurePosixPath(rel),
        direction="a_to_b",
        reason="",
        size=0,
        src=None,
        dst=None,
    )


def test_ordering_is_mkdir_then_copies_then_file_deletes_then_deep_dirs_first() -> None:
    plan = Plan(
        actions=[
            action("delete_dir", "x/y"),
            action("delete_file", "x/y/f.txt"),
            action("copy_new", "a/b.txt"),
            action("delete_dir", "x"),
            action("mkdir", "a/deep"),
            action("mkdir", "a"),
        ]
    )
    assert [str(a.rel) for a in ordered_actions(plan.actions)] == [
        "a",
        "a/deep",
        "a/b.txt",
        "x/y/f.txt",
        "x/y",
        "x",
    ]


def test_excluded_rows_are_not_executed(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"yes.txt": "y", "no.txt": "n"})
    b = build(tmp_path / "b", {})
    job = config(a, b)
    plan, _, _ = preview(job)
    for index, act in enumerate(plan.actions):
        if act.rel.name == "no.txt":
            plan.set_included(index, False)
    execute(plan, job)
    assert (b / "yes.txt").exists()
    assert not (b / "no.txt").exists()


def test_mirror_run_copies_creates_and_deletes(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"keep.txt": "k", "sub/new.txt": "n", "empty": None})
    b = build(tmp_path / "b", {"stale.txt": "s", "gonedir": None})
    plan, result = run(a, b, deletion_policy="permanent")
    assert (b / "sub" / "new.txt").read_text() == "n"
    assert (b / "empty").is_dir()
    assert not (b / "stale.txt").exists()
    assert not (b / "gonedir").exists()
    assert (result.copied, result.deleted, result.failed) == (2, 2, 0)
    assert result.elapsed_s >= 0


def test_copy_preserves_the_modification_time(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "data"})
    b = build(tmp_path / "b", {})
    os.utime(a / "f.txt", ns=(1_700_000_000_000000000, 1_700_000_000_000000000))
    run(a, b)
    assert (b / "f.txt").stat().st_mtime_ns == (a / "f.txt").stat().st_mtime_ns


def test_deletes_go_to_quarantine_by_default(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"sub/old.txt": "o"})
    run(a, b)
    assert not (b / "sub" / "old.txt").exists()
    quarantined = list((b / ".deleted").rglob("old.txt"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "o"


def test_directories_are_only_removed_once_their_contents_are_gone(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"outer/inner/f.txt": "x"})
    _, result = run(a, b, deletion_policy="permanent")
    assert not (b / "outer").exists()
    assert result.failed == 0


def test_an_interrupted_copy_leaves_no_partial_target_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = build(tmp_path / "a", {"big.txt": "payload", "fine.txt": "ok"})
    b = build(tmp_path / "b", {})

    real_copystat = shutil.copystat

    def explode(src, dst, **kwargs):
        if Path(src).name == "big.txt":
            raise OSError(5, "Input/output error")
        return real_copystat(src, dst, **kwargs)

    monkeypatch.setattr(executor.shutil, "copystat", explode)
    _, result = run(a, b)

    assert not (b / "big.txt").exists()
    assert not (b / ("big.txt" + TEMP_SUFFIX)).exists()
    assert (b / "fine.txt").read_text() == "ok"  # the run carried on
    assert result.failed == 1
    assert result.errors[0].rel == "big.txt"


@POSIX_ONLY
def test_a_permission_error_on_one_file_does_not_stop_the_rest(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"locked.txt": "l", "one.txt": "1", "two.txt": "2"})
    b = build(tmp_path / "b", {})
    os.chmod(a / "locked.txt", 0o000)
    try:
        _, result = run(a, b)
    finally:
        os.chmod(a / "locked.txt", 0o644)
    assert (b / "one.txt").exists() and (b / "two.txt").exists()
    assert result.copied == 2
    assert result.failed == 1
    assert result.errors[0].rel == "locked.txt"
    assert not (b / "locked.txt").exists()


@POSIX_ONLY
def test_a_read_only_destination_file_is_reported_not_fatal(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "new content"})
    b = build(tmp_path / "b", {"f.txt": "old"})
    os.chmod(b / "f.txt", 0o444)
    os.chmod(b, 0o555)
    try:
        _, result = run(a, b)
    finally:
        os.chmod(b, 0o755)
        os.chmod(b / "f.txt", 0o644)
    assert result.failed == 1
    assert (b / "f.txt").read_text() == "old"


def test_cancelling_before_the_first_item_writes_nothing(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "x"})
    b = build(tmp_path / "b", {})
    cancel = threading.Event()
    cancel.set()
    _, result = run(a, b, cancel=cancel)
    assert result.cancelled
    assert list(b.iterdir()) == []


def test_cancelling_mid_copy_leaves_no_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor, "COPY_CHUNK", 4)
    a = build(tmp_path / "a", {"big.txt": "x" * 4096})
    b = build(tmp_path / "b", {})
    cancel = threading.Event()

    def watch(event: RunEvent) -> None:
        if event.kind == "progress":
            cancel.set()

    _, result = run(a, b, cancel=cancel, on_event=watch)
    assert result.cancelled
    assert not (b / "big.txt").exists()
    assert not (b / ("big.txt" + TEMP_SUFFIX)).exists()


def test_events_describe_the_run(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "hello"})
    b = build(tmp_path / "b", {})
    events: list[RunEvent] = []
    run(a, b, on_event=events.append)
    kinds = [event.kind for event in events]
    assert kinds[0] == "started"
    assert kinds[-1] == "done"
    assert events[-1].result is not None and events[-1].result.copied == 1
    assert events[0].total_bytes == 5


@POSIX_ONLY
def test_a_symlink_is_recreated_rather_than_copied_through(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"real.txt": "data"})
    (a / "link.txt").symlink_to(a / "real.txt")
    b = build(tmp_path / "b", {})
    run(a, b)
    assert (b / "link.txt").is_symlink()
    assert os.readlink(b / "link.txt") == str(a / "real.txt")


def test_a_kind_conflict_replaces_the_destination_under_the_policy(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"thing": "now a file"})
    b = build(tmp_path / "b", {"thing/inside.txt": "was a folder"})
    plan, result = run(a, b)
    assert [act.kind for act in plan.actions if str(act.rel) == "thing"] == ["conflict"]
    assert (b / "thing").read_text() == "now a file"
    assert list((b / ".deleted").rglob("inside.txt"))
    assert result.failed == 0
