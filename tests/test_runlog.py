"""The per-run log: a line per operation, written before it and finished after it."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest
from helpers import build

from dirsynk.core import executor, jobs, runlog
from dirsynk.core.executor import execute
from dirsynk.core.models import JobConfig
from dirsynk.core.planner import preview

POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")


def run(a: Path, b: Path, *, name: str = "Photos to NAS", save: bool = True, **kwargs):
    job = JobConfig(name=name, path_a=str(a), path_b=str(b), exclude=[], **kwargs)
    if save:
        jobs.save(job)
    plan, _, _ = preview(job)
    return job, execute(plan, job, stamp="20260905-142530")


def only_log(home: Path) -> list[str]:
    """The lines of the single log file the run wrote."""
    logs = sorted(home.glob("*.log"))
    assert len(logs) == 1, logs
    return logs[0].read_text(encoding="utf-8").splitlines()


def operations(lines: list[str]) -> list[str]:
    return [line for line in lines if not line.startswith("#")]


# --- where it goes and what it is called ---------------------------------------------


def test_the_log_is_named_after_the_job_file_and_the_moment_the_run_started(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"one.txt": "1"})
    job, result = run(a, tmp_path / "b")

    expected = isolated_jobs_dir / "photos-to-nas-20260905-142530.log"
    assert job.file_path is not None and job.file_path.name == "photos-to-nas.json"
    assert expected.is_file()
    assert result.log_path == str(expected)


def test_a_job_that_was_never_saved_borrows_the_name_its_file_would_have(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"one.txt": "1"})
    run(a, tmp_path / "b", name="Never Saved", save=False)

    assert (isolated_jobs_dir / "never-saved-20260905-142530.log").is_file()


def test_the_log_sits_beside_the_job_files_and_shares_the_deleted_folders_stamp(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"old.txt": "x"})
    run(a, b)

    assert (isolated_jobs_dir / "photos-to-nas-20260905-142530.log").parent == jobs.jobs_dir()
    assert (b / ".deleted" / "20260905-142530" / "old.txt").is_file()


# --- one line per operation ----------------------------------------------------------


def test_every_copy_folder_and_delete_gets_a_line_with_both_paths_and_success(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"sub/deep.txt": "d", "top.txt": "t"})
    b = build(tmp_path / "b", {"stale.txt": "s"})
    run(a, b)

    lines = operations(only_log(isolated_jobs_dir))
    assert len(lines) == 4
    assert all(line.endswith("  success") for line in lines)

    kinds = [line.split()[2] for line in lines]
    assert kinds == ["create", "copy", "copy", "delete"]  # "create dir" splits in two

    folder = next(line for line in lines if "create dir" in line)
    assert f"{a / 'sub'}  ->  {b / 'sub'}" in folder
    copy = next(line for line in lines if str(a / "top.txt") in line)
    assert f"{a / 'top.txt'}  ->  {b / 'top.txt'}" in copy


def test_a_quarantined_delete_records_where_the_item_went(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"old.txt": "x"})
    run(a, b)

    line = operations(only_log(isolated_jobs_dir))[0]
    quarantined = b / ".deleted" / "20260905-142530" / "old.txt"
    assert f"delete      {b / 'old.txt'}  ->  {quarantined}  success" in line


def test_a_permanent_delete_has_no_destination_to_record(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {})
    b = build(tmp_path / "b", {"old.txt": "x"})
    run(a, b, deletion_policy="permanent")

    line = operations(only_log(isolated_jobs_dir))[0]
    assert f"delete      {b / 'old.txt'}  ->  -  success" in line


def test_nothing_to_do_still_writes_a_log_with_a_header_and_a_summary(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"same.txt": "s"})
    b = build(tmp_path / "b", {"same.txt": "s"})
    run(a, b)

    lines = only_log(isolated_jobs_dir)
    assert operations(lines) == []
    assert lines[0].startswith("# dirsynk run — Photos to NAS — started ")
    assert lines[-1].endswith("0 operations, 0 succeeded, 0 failed")


# --- the outcome is appended to the line the operation already wrote -------------------


def test_the_operation_is_on_the_page_before_it_is_attempted(
    tmp_path: Path, isolated_jobs_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The half-line is flushed first, so a run killed mid-copy still names the file."""
    seen: list[str] = []
    real_copy = executor._copy_file

    def watched(src: Path, dst: Path, **kwargs):
        seen.append((isolated_jobs_dir / "photos-to-nas-20260905-142530.log").read_text())
        return real_copy(src, dst, **kwargs)

    monkeypatch.setattr(executor, "_copy_file", watched)
    a = build(tmp_path / "a", {"one.txt": "1"})
    run(a, tmp_path / "b")

    mid_copy = operations(seen[0].splitlines())[0]
    assert str(a / "one.txt") in mid_copy
    assert "success" not in mid_copy  # the status is not written until it is earned


@POSIX_ONLY
def test_a_failed_operation_records_the_error_after_the_two_paths(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"locked.txt": "l", "fine.txt": "f"})
    b = build(tmp_path / "b", {})
    os.chmod(a / "locked.txt", 0o000)
    try:
        _, result = run(a, b)
    finally:
        os.chmod(a / "locked.txt", 0o644)

    assert result.failed == 1
    lines = operations(only_log(isolated_jobs_dir))
    failure = next(line for line in lines if "locked.txt" in line)
    assert f"{a / 'locked.txt'}  ->  {b / 'locked.txt'}  failed: Permission denied" in failure
    assert any(line.endswith("  success") for line in lines)


class _CancelDuringCopy(threading.Event):
    """Not cancelled when the loop checks, cancelled by the time the copy reads a chunk."""

    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    def is_set(self) -> bool:
        self.checks += 1
        return self.checks > 1


def test_a_copy_stopped_part_way_is_marked_rather_than_left_hanging(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"one.txt": "1"})
    job = JobConfig(name="Photos to NAS", path_a=str(a), path_b=str(tmp_path / "b"), exclude=[])
    plan, _, _ = preview(job)

    execute(plan, job, stamp="20260905-142530", cancel=_CancelDuringCopy())

    lines = operations(only_log(isolated_jobs_dir))
    assert lines[0].endswith("  cancelled")


# --- the tally at the end -------------------------------------------------------------


@POSIX_ONLY
def test_the_summary_counts_operations_successes_and_failures(
    tmp_path: Path, isolated_jobs_dir: Path
) -> None:
    a = build(tmp_path / "a", {"locked.txt": "l", "one.txt": "1", "two.txt": "2"})
    b = build(tmp_path / "b", {})
    os.chmod(a / "locked.txt", 0o000)
    try:
        run(a, b)
    finally:
        os.chmod(a / "locked.txt", 0o644)

    assert only_log(isolated_jobs_dir)[-1].endswith("3 operations, 2 succeeded, 1 failed")


def test_an_unfinished_operation_is_counted_separately_from_the_two_outcomes() -> None:
    log = runlog.RunLog(Path("unused"))
    log.begin(runlog.COPY, Path("/a"), Path("/b"))
    log.success()
    log.begin(runlog.COPY, Path("/a"), Path("/b"))
    log.cancelled()

    assert log.summary_line() == "2 operations, 1 succeeded, 0 failed, 1 not finished"


def test_finishing_a_line_that_was_never_begun_changes_nothing() -> None:
    """The executor's except clause fires for failures that never opened a line."""
    log = runlog.RunLog(Path("unused"))
    log.failure("boom")
    log.success()

    assert log.summary_line() == "0 operations, 0 succeeded, 0 failed"


# --- the log must never cost the run --------------------------------------------------


def test_a_log_that_cannot_be_written_is_reported_but_the_files_still_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("in the way", encoding="utf-8")
    monkeypatch.setenv(jobs.HOME_ENV_VAR, str(blocked))

    a = build(tmp_path / "a", {"one.txt": "1"})
    b = tmp_path / "b"
    job = JobConfig(name="Blocked", path_a=str(a), path_b=str(b), exclude=[])
    plan, _, _ = preview(job)
    result = execute(plan, job, stamp="20260905-142530")

    assert (b / "one.txt").read_text(encoding="utf-8") == "1"
    assert result.copied == 1 and result.failed == 0
    assert result.log_error is not None and "could not write" in result.log_error
