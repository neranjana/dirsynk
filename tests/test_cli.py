from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import build, set_mtime

from dirsynk.cli import main
from dirsynk.core import jobs

SECOND = 1_000_000_000
BASE = 1_700_000_000 * SECOND


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "dirsynk-home"
    monkeypatch.setenv(jobs.HOME_ENV_VAR, str(home))
    return home


def fixture_job(tmp_path: Path, **kwargs) -> tuple[Path, Path]:
    """A tree with one new file, one same-size edit, one extra file on the far side."""
    a = build(tmp_path / "a", {"new.txt": "brand new", "edited.txt": "aaaa", "same.txt": "keep"})
    b = build(tmp_path / "b", {"edited.txt": "bbbb", "same.txt": "keep", "extra.txt": "stale"})
    set_mtime(a / "edited.txt", BASE + 3600 * SECOND)
    set_mtime(b / "edited.txt", BASE)
    set_mtime(a / "same.txt", BASE)
    set_mtime(b / "same.txt", BASE)
    job = jobs.new_job("Fixture", str(a), str(b))
    job.exclude = []
    for key, value in kwargs.items():
        setattr(job, key, value)
    jobs.save(job)
    return a, b


def test_dry_run_prints_the_plan_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    a, b = fixture_job(tmp_path)
    before = sorted(p.name for p in b.iterdir())

    assert main(["--job", "Fixture", "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "Job: Fixture" in out
    assert "Compared by: size and timestamp" in out
    assert "copy_new     A → B   new.txt" in out
    assert "copy_update  A → B   edited.txt" in out
    assert "delete_file  in B    extra.txt" in out
    assert "same.txt" not in out
    assert "2 to copy (13 B) · 1 to delete (5 B)" in out
    assert "Dry run: nothing was written." in out
    assert sorted(p.name for p in b.iterdir()) == before


def test_compare_size_only_changes_the_plan(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    fixture_job(tmp_path)

    assert main(["--job", "Fixture", "--dry-run", "--compare", "size_only"]) == 0

    out = capsys.readouterr().out
    assert "Compared by: size only" in out
    # The same-size edit is invisible to this criterion; the new and extra files are not.
    assert "edited.txt" not in out
    assert "copy_new     A → B   new.txt" in out
    assert "1 to copy (9 B) · 1 to delete (5 B)" in out


def test_compare_override_is_never_written_back_to_the_job(tmp_path: Path) -> None:
    fixture_job(tmp_path)
    main(["--job", "Fixture", "--dry-run", "--compare", "content"])
    saved = json.loads((jobs.jobs_dir() / "fixture.json").read_text())
    assert saved["compare"] == "size_mtime"


def test_yes_runs_the_plan_and_records_the_result(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    a, b = fixture_job(tmp_path)

    assert main(["--job", "Fixture", "--yes"]) == 0

    assert (b / "new.txt").read_text() == "brand new"
    assert (b / "edited.txt").read_text() == "aaaa"
    assert not (b / "extra.txt").exists()
    assert list((b / ".deleted").rglob("extra.txt"))

    out = capsys.readouterr().out
    assert "1 item(s) (5 B) will be moved to .deleted." in out
    assert "2 copied" in out

    saved = json.loads((jobs.jobs_dir() / "fixture.json").read_text())
    assert saved["last_run_utc"] is not None
    assert saved["last_result"]["copied"] == 2
    assert saved["snapshot"] == {}  # mirror mode keeps no snapshot


def test_permanent_deletion_says_so_more_loudly(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    fixture_job(tmp_path, deletion_policy="permanent")
    main(["--job", "Fixture", "--yes"])
    assert "permanently deleted. This cannot be undone." in capsys.readouterr().out


def test_declining_the_prompt_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    a, b = fixture_job(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    assert main(["--job", "Fixture"]) == 130

    assert not (b / "new.txt").exists()
    assert (b / "extra.txt").exists()
    assert "Nothing was written." in capsys.readouterr().out


def test_two_way_run_stores_a_snapshot(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"a_only.txt": "a"})
    b = build(tmp_path / "b", {"b_only.txt": "b"})
    job = jobs.new_job("Both ways", str(a), str(b))
    job.mode = "two_way"
    job.exclude = []
    jobs.save(job)

    assert main(["--job", "Both ways", "--yes"]) == 0

    assert (b / "a_only.txt").exists() and (a / "b_only.txt").exists()
    saved = json.loads((jobs.jobs_dir() / "both-ways.json").read_text())
    assert set(saved["snapshot"]) == {"a_only.txt", "b_only.txt"}
    assert saved["snapshot"]["a_only.txt"]["kind"] == "file"


def test_two_way_first_run_says_it_will_not_delete(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    a = build(tmp_path / "a", {"a_only.txt": "a"})
    b = build(tmp_path / "b", {})
    job = jobs.new_job("Both ways", str(a), str(b))
    job.mode = "two_way"
    jobs.save(job)
    main(["--job", "Both ways", "--dry-run"])
    assert "First two-way run" in capsys.readouterr().out


def test_nothing_to_do_is_a_valid_outcome(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    a = build(tmp_path / "a", {"f.txt": "same"})
    b = build(tmp_path / "b", {"f.txt": "same"})
    set_mtime(a / "f.txt", BASE)
    set_mtime(b / "f.txt", BASE)
    job = jobs.new_job("Quiet", str(a), str(b))
    jobs.save(job)

    assert main(["--job", "Quiet"]) == 0

    out = capsys.readouterr().out
    assert "Nothing to do." in out


def test_an_unknown_job_is_reported(capsys: pytest.CaptureFixture) -> None:
    assert main(["--job", "ghost"]) == 1
    assert "No job named 'ghost'" in capsys.readouterr().err


def test_a_job_with_a_missing_folder_refuses_to_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    job = jobs.new_job("Broken", str(tmp_path / "gone"), str(tmp_path))
    jobs.save(job)
    assert main(["--job", "Broken"]) == 1
    assert "path not found" in capsys.readouterr().err


def test_list_shows_saved_jobs(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    fixture_job(tmp_path)
    assert main(["--list"]) == 0
    assert "Fixture  [mirror, size_mtime]" in capsys.readouterr().out


def test_a_failing_item_gives_a_partial_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture_job(tmp_path)
    import dirsynk.core.executor as executor_module

    def explode(src, dst, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(executor_module.shutil, "copystat", explode)
    assert main(["--job", "Fixture", "--yes"]) == 3


def test_requires_a_job_or_list(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit):
        main(["--yes"])


def test_a_job_can_be_run_reopened_and_run_again(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    a, b = fixture_job(tmp_path)

    assert main(["--job", "Fixture", "--yes"]) == 0
    capsys.readouterr()

    # Reopened from ~/.dirsynk, the second run has nothing left to do.
    assert main(["--job", "Fixture", "--yes"]) == 0
    assert "Nothing to do." in capsys.readouterr().out


def test_two_way_second_run_propagates_a_delete(tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"shared.txt": "s", "keep.txt": "k"})
    b = build(tmp_path / "b", {})
    job = jobs.new_job("Both ways", str(a), str(b))
    job.mode = "two_way"
    job.exclude = []
    jobs.save(job)

    assert main(["--job", "Both ways", "--yes"]) == 0
    assert (b / "shared.txt").exists()

    (b / "shared.txt").unlink()
    assert main(["--job", "Both ways", "--yes"]) == 0

    # The delete travelled back to A, and the untouched file stayed put.
    assert not (a / "shared.txt").exists()
    assert (a / "keep.txt").exists()
    assert list((a / ".deleted").rglob("shared.txt"))
    saved = json.loads((jobs.jobs_dir() / "both-ways.json").read_text())
    assert set(saved["snapshot"]) == {"keep.txt"}


def test_the_plan_says_how_long_the_pausing_will_add(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    fixture_job(tmp_path, copy_pause_s=5)
    assert main(["--job", "Fixture", "--dry-run"]) == 0
    # Two copies in the fixture: one gap of five seconds between them.
    assert "Pausing 5s between file copies (adds about 5.0s)" in capsys.readouterr().out


def test_a_job_without_a_pause_says_nothing_about_pausing(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    fixture_job(tmp_path)
    assert main(["--job", "Fixture", "--dry-run"]) == 0
    assert "Pausing" not in capsys.readouterr().out


def test_pause_overrides_the_job_for_one_run_and_is_never_written_back(
    tmp_path: Path, isolated_home: Path, capsys: pytest.CaptureFixture
) -> None:
    fixture_job(tmp_path, copy_pause_s=5)
    assert main(["--job", "Fixture", "--pause", "0", "--yes"]) == 0

    assert "Pausing" not in capsys.readouterr().out
    saved = json.loads((isolated_home / "fixture.json").read_text(encoding="utf-8"))
    assert saved["copy_pause_s"] == 5


def test_an_impossible_pause_is_refused_before_anything_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    fixture_job(tmp_path)
    for bad in ("-1", "nonsense", "99999"):
        with pytest.raises(SystemExit) as exit_info:
            main(["--job", "Fixture", "--pause", bad, "--dry-run"])
        assert exit_info.value.code == 2
