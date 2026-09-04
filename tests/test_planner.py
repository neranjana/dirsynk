from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from helpers import build, set_mtime

from dirsynk.core.models import Action, JobConfig, Plan
from dirsynk.core.planner import build_snapshot, preview

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND


def config(a: Path, b: Path, **kwargs) -> JobConfig:
    return JobConfig(name="test", path_a=str(a), path_b=str(b), exclude=[], **kwargs)


def plan_for(a: Path, b: Path, *, hasher: Callable[[Path], str] | None = None, **kwargs) -> Plan:
    plan, _, _ = preview(config(a, b, **kwargs), hasher=hasher)
    return plan


def by_rel(plan: Plan) -> dict[str, Action]:
    return {str(action.rel): action for action in plan.actions}


# --- mirror -----------------------------------------------------------------------


def test_mirror_copies_a_new_file(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"new.txt": "hello"})
    b = build(tmp_path / "b", {})
    action = by_rel(plan_for(a, b))["new.txt"]
    assert action.kind == "copy_new"
    assert action.direction == "a_to_b"
    assert action.reason == "new in A"
    assert action.src == a / "new.txt"
    assert action.dst == b / "new.txt"


def test_mirror_updates_a_changed_file(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "longer content"})
    b = build(tmp_path / "b", {"f.txt": "short"})
    action = by_rel(plan_for(a, b))["f.txt"]
    assert action.kind == "copy_update"
    assert action.reason.startswith("sizes differ")


def test_mirror_leaves_identical_files_alone(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "same"})
    b = build(tmp_path / "b", {"f.txt": "same"})
    set_mtime(a / "f.txt", BASE)
    set_mtime(b / "f.txt", BASE)
    plan = plan_for(a, b)
    assert plan.actions == []
    assert plan.summary_line() == "Nothing to do"


def test_mirror_deletes_a_file_missing_from_the_source(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"extra.txt": "x"})
    action = by_rel(plan_for(a, b))["extra.txt"]
    assert action.kind == "delete_file"
    assert action.reason == "not in A"
    assert action.dst == b / "extra.txt"


def test_mirror_creates_an_empty_directory(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"empty": None})
    b = build(tmp_path / "b", {})
    action = by_rel(plan_for(a, b))["empty"]
    assert action.kind == "mkdir"
    assert action.size == 0


def test_mirror_removes_an_empty_directory(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"gone": None})
    assert by_rel(plan_for(a, b))["gone"].kind == "delete_dir"


def test_mirror_treats_a_kind_mismatch_as_a_conflict(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"thing": "I am a file"})
    b = build(tmp_path / "b", {"thing": None})
    action = by_rel(plan_for(a, b))["thing"]
    assert action.kind == "conflict"
    assert action.reason == "file in A, folder in B"


def test_mirror_conflict_keeps_its_kind_when_the_source_is_a_directory(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"thing": None})
    b = build(tmp_path / "b", {"thing": "I am a file"})
    assert by_rel(plan_for(a, b))["thing"].kind == "conflict"


# --- the comparison criteria ------------------------------------------------------


def _same_size_different_mtime(tmp_path: Path, *, same_bytes: bool) -> tuple[Path, Path]:
    a = build(tmp_path / "a", {"f.txt": "aaaa"})
    b = build(tmp_path / "b", {"f.txt": "aaaa" if same_bytes else "bbbb"})
    set_mtime(a / "f.txt", BASE + 3600 * SECOND)
    set_mtime(b / "f.txt", BASE)
    return a, b


def test_same_size_different_mtime_copies_under_size_mtime(tmp_path: Path) -> None:
    a, b = _same_size_different_mtime(tmp_path, same_bytes=True)
    action = by_rel(plan_for(a, b, compare="size_mtime"))["f.txt"]
    assert action.kind == "copy_update"
    assert action.reason == "A newer by 1.0h"


def test_same_size_different_mtime_does_nothing_under_size_only(tmp_path: Path) -> None:
    a, b = _same_size_different_mtime(tmp_path, same_bytes=True)
    assert plan_for(a, b, compare="size_only").actions == []


def test_content_ignores_a_pure_timestamp_difference(tmp_path: Path) -> None:
    a, b = _same_size_different_mtime(tmp_path, same_bytes=True)
    assert plan_for(a, b, compare="content").actions == []


def test_content_catches_a_same_size_edit(tmp_path: Path) -> None:
    a, b = _same_size_different_mtime(tmp_path, same_bytes=False)
    action = by_rel(plan_for(a, b, compare="content"))["f.txt"]
    assert action.kind == "copy_update"
    assert action.reason == "same size, contents differ"


def test_size_only_cannot_see_a_same_size_edit(tmp_path: Path) -> None:
    a, b = _same_size_different_mtime(tmp_path, same_bytes=False)
    assert plan_for(a, b, compare="size_only").actions == []


@pytest.mark.parametrize("compare", ["size_mtime", "size_only", "content"])
def test_different_size_same_mtime_copies_under_every_criterion(tmp_path: Path, compare) -> None:
    a = build(tmp_path / "a", {"f.txt": "aaaaaaaa"})
    b = build(tmp_path / "b", {"f.txt": "aaaa"})
    set_mtime(a / "f.txt", BASE)
    set_mtime(b / "f.txt", BASE)
    action = by_rel(plan_for(a, b, compare=compare))["f.txt"]
    assert action.kind == "copy_update"
    assert action.reason == "sizes differ (8 B vs 4 B)"


def test_mtime_within_tolerance_is_not_a_difference(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "aaaa"})
    b = build(tmp_path / "b", {"f.txt": "aaaa"})
    set_mtime(a / "f.txt", BASE + 1 * SECOND)
    set_mtime(b / "f.txt", BASE)
    assert plan_for(a, b, compare="size_mtime", mtime_tolerance_s=2).actions == []


def test_mtime_beyond_tolerance_is_a_difference(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "aaaa"})
    b = build(tmp_path / "b", {"f.txt": "aaaa"})
    set_mtime(a / "f.txt", BASE + 3 * SECOND)
    set_mtime(b / "f.txt", BASE)
    assert by_rel(plan_for(a, b, mtime_tolerance_s=2))["f.txt"].kind == "copy_update"


@pytest.mark.parametrize("compare", ["size_only", "content"])
def test_tolerance_is_ignored_by_the_other_two_criteria(tmp_path: Path, compare) -> None:
    a = build(tmp_path / "a", {"f.txt": "aaaa"})
    b = build(tmp_path / "b", {"f.txt": "aaaa"})
    set_mtime(a / "f.txt", BASE + 10_000 * SECOND)
    set_mtime(b / "f.txt", BASE)
    assert plan_for(a, b, compare=compare, mtime_tolerance_s=0).actions == []


class HashSpy:
    def __init__(self) -> None:
        self.calls: list[Path] = []

    def __call__(self, path: Path) -> str:
        self.calls.append(path)
        return path.read_bytes().hex()


def test_content_never_hashes_files_of_differing_size(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"big.txt": "aaaaaaaa", "same.txt": "xxxx"})
    b = build(tmp_path / "b", {"big.txt": "aaaa", "same.txt": "xxxx"})
    spy = HashSpy()
    plan_for(a, b, compare="content", hasher=spy)
    assert all("big.txt" not in str(path) for path in spy.calls)
    assert any("same.txt" in str(path) for path in spy.calls)


def test_content_hashes_each_file_at_most_once_per_plan(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "aaaa", "g.txt": "bbbb"})
    b = build(tmp_path / "b", {"f.txt": "aaaa", "g.txt": "bbbb"})
    spy = HashSpy()
    plan_for(a, b, compare="content", hasher=spy)
    assert len(spy.calls) == len(set(spy.calls))


def test_directories_are_never_hashed(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"d/f.txt": "aaaa"})
    b = build(tmp_path / "b", {"d/f.txt": "aaaa"})
    spy = HashSpy()
    plan_for(a, b, compare="content", hasher=spy)
    assert all(path.is_file() for path in spy.calls)


# --- two-way ----------------------------------------------------------------------


def two_way(a: Path, b: Path, snapshot: dict | None = None, **kwargs) -> Plan:
    return plan_for(a, b, mode="two_way", snapshot=snapshot or {}, **kwargs)


def snapshot_of(a: Path, b: Path, **kwargs) -> dict:
    _, scan_a, scan_b = preview(config(a, b, **kwargs))
    return build_snapshot(scan_a.entries, scan_b.entries)


def test_two_way_first_run_copies_both_ways_and_deletes_nothing(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"only_a.txt": "a"})
    b = build(tmp_path / "b", {"only_b.txt": "b"})
    plan = two_way(a, b)
    assert plan.first_run
    assert {k: v.kind for k, v in by_rel(plan).items()} == {
        "only_a.txt": "copy_new",
        "only_b.txt": "copy_new",
    }
    assert by_rel(plan)["only_b.txt"].direction == "b_to_a"
    assert plan.n_deletes == 0


def test_two_way_propagates_a_delete(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"shared.txt": "s"})
    b = build(tmp_path / "b", {"shared.txt": "s"})
    snapshot = snapshot_of(a, b)
    (b / "shared.txt").unlink()
    action = by_rel(two_way(a, b, snapshot))["shared.txt"]
    assert action.kind == "delete_file"
    assert action.reason == "removed from B"
    assert action.direction == "b_to_a"  # the write lands on A
    assert action.dst == a / "shared.txt"


def test_two_way_propagates_an_edit(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"shared.txt": "s"})
    b = build(tmp_path / "b", {"shared.txt": "s"})
    set_mtime(a / "shared.txt", BASE)
    set_mtime(b / "shared.txt", BASE)
    snapshot = snapshot_of(a, b)
    (b / "shared.txt").write_text("edited on b")
    set_mtime(b / "shared.txt", BASE + 100 * SECOND)
    action = by_rel(two_way(a, b, snapshot))["shared.txt"]
    assert action.kind == "copy_update"
    assert action.direction == "b_to_a"


def test_two_way_simultaneous_edits_are_a_conflict(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"shared.txt": "s"})
    b = build(tmp_path / "b", {"shared.txt": "s"})
    set_mtime(a / "shared.txt", BASE)
    set_mtime(b / "shared.txt", BASE)
    snapshot = snapshot_of(a, b)
    (a / "shared.txt").write_text("edited on a")
    set_mtime(a / "shared.txt", BASE + 50 * SECOND)
    (b / "shared.txt").write_text("edited on b, and longer")
    set_mtime(b / "shared.txt", BASE + 100 * SECOND)
    action = by_rel(two_way(a, b, snapshot))["shared.txt"]
    assert action.kind == "conflict"
    assert action.direction == "b_to_a"  # newer wins by default
    assert action.reason.startswith("both sides changed since last sync")


def test_two_way_new_file_on_one_side_is_copied_not_deleted(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"shared.txt": "s"})
    b = build(tmp_path / "b", {"shared.txt": "s"})
    snapshot = snapshot_of(a, b)
    (a / "fresh.txt").write_text("new")
    action = by_rel(two_way(a, b, snapshot))["fresh.txt"]
    assert action.kind == "copy_new"
    assert action.reason == "new in A"


def test_two_way_size_only_uses_mtime_to_pick_the_direction(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "aaaa"})
    b = build(tmp_path / "b", {"f.txt": "bbbbbbbb"})
    set_mtime(a / "f.txt", BASE + 52 * 60 * SECOND)
    set_mtime(b / "f.txt", BASE)
    action = by_rel(two_way(a, b, compare="size_only"))["f.txt"]
    assert action.direction == "a_to_b"
    assert action.reason == "sizes differ (4 B vs 8 B), A newer by 52m"


def test_two_way_empty_directory_added_on_one_side(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"newdir": None})
    b = build(tmp_path / "b", {})
    assert by_rel(two_way(a, b))["newdir"].kind == "mkdir"


def test_two_way_empty_directory_removed_on_one_side(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"d": None})
    b = build(tmp_path / "b", {"d": None})
    snapshot = snapshot_of(a, b)
    (b / "d").rmdir()
    action = by_rel(two_way(a, b, snapshot))["d"]
    assert action.kind == "delete_dir"
    assert action.reason == "removed from B"


def test_build_snapshot_records_only_what_both_sides_agree_on(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"shared.txt": "s", "only_a.txt": "a"})
    b = build(tmp_path / "b", {"shared.txt": "s"})
    snapshot = snapshot_of(a, b)
    assert set(snapshot) == {"shared.txt"}
    assert snapshot["shared.txt"]["kind"] == "file"


def test_plan_records_the_active_criterion_and_scan_counts(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "x"})
    b = build(tmp_path / "b", {})
    plan = plan_for(a, b, compare="size_only")
    assert plan.compare == "size_only"
    assert plan.compared_by() == "Compared by: size only"
    assert (plan.scanned_a, plan.scanned_b) == (1, 0)


def test_compare_override_does_not_touch_the_job(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "x"})
    b = build(tmp_path / "b", {})
    job = config(a, b, compare="size_mtime")
    plan, _, _ = preview(job, compare="content")
    assert plan.compare == "content"
    assert job.compare == "size_mtime"
