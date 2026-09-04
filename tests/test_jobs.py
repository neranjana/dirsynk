from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from helpers import build

from dirsynk.core import jobs
from dirsynk.core.jobs import JobError
from dirsynk.core.models import JobConfig


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "dirsynk-home"
    monkeypatch.setenv(jobs.HOME_ENV_VAR, str(home))
    return home


def a_job(**kwargs) -> JobConfig:
    job = jobs.new_job("Photos to NAS", "/tmp/photos", "/tmp/nas")
    for key, value in kwargs.items():
        setattr(job, key, value)
    return job


def test_save_creates_the_jobs_directory_and_round_trips(isolated_home: Path) -> None:
    job = a_job(compare="content", mtime_tolerance_s=5, exclude=["*.tmp"])
    path = jobs.save(job)
    assert path.parent == isolated_home
    assert path.name == "photos-to-nas.json"

    reloaded = jobs.load(path)
    assert reloaded.name == "Photos to NAS"
    assert reloaded.compare == "content"
    assert reloaded.mtime_tolerance_s == 5
    assert reloaded.exclude == ["*.tmp"]
    assert reloaded.file_path == path


def test_saved_file_is_indented_utf8_json_with_the_schema_version(isolated_home: Path) -> None:
    path = jobs.save(a_job(name="Café backup"))
    text = path.read_text(encoding="utf-8")
    assert '"schema_version": 1' in text
    assert "Café" in text
    assert json.loads(text)["snapshot"] == {}


def test_saving_again_overwrites_the_same_file(isolated_home: Path) -> None:
    job = a_job()
    first = jobs.save(job)
    job.compare = "size_only"
    second = jobs.save(job)
    assert first == second
    assert jobs.load(first).compare == "size_only"


def test_a_failed_write_leaves_no_temp_file_behind(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs.ensure_jobs_dir()
    monkeypatch.setattr(
        jobs.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    with pytest.raises(JobError):
        jobs.save(a_job())
    assert list(isolated_home.glob("*.tmp")) == []


def test_the_original_survives_a_failed_rewrite(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = a_job()
    path = jobs.save(job)
    monkeypatch.setattr(
        jobs.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(13, "denied"))
    )
    job.compare = "content"
    with pytest.raises(JobError):
        jobs.save(job)
    assert jobs.load(path).compare == "size_mtime"


def test_unknown_schema_version_is_rejected_with_a_clear_message(isolated_home: Path) -> None:
    path = jobs.save(a_job())
    data = json.loads(path.read_text())
    data["schema_version"] = 7
    path.write_text(json.dumps(data))
    with pytest.raises(JobError, match="schema_version"):
        jobs.load(path)


def test_unknown_compare_value_is_rejected_never_silently_defaulted(isolated_home: Path) -> None:
    path = jobs.save(a_job())
    data = json.loads(path.read_text())
    data["compare"] = "fuzzy"
    path.write_text(json.dumps(data))
    with pytest.raises(JobError, match="compare"):
        jobs.load(path)


def test_a_file_with_no_compare_key_loads_as_size_mtime(isolated_home: Path) -> None:
    path = jobs.save(a_job())
    data = json.loads(path.read_text())
    del data["compare"]
    path.write_text(json.dumps(data))
    assert jobs.load(path).compare == "size_mtime"


def test_tolerance_is_kept_even_when_the_criterion_ignores_it(isolated_home: Path) -> None:
    path = jobs.save(a_job(compare="size_only", mtime_tolerance_s=9))
    assert json.loads(path.read_text())["mtime_tolerance_s"] == 9
    assert jobs.load(path).mtime_tolerance_s == 9


def test_a_job_whose_directories_are_gone_still_opens(isolated_home: Path) -> None:
    path = jobs.save(a_job(path_a="/nowhere/at/all", path_b="/also/nowhere"))
    reloaded = jobs.load(path)
    assert reloaded.path_a == "/nowhere/at/all"


def test_invalid_json_and_missing_fields_are_reported(isolated_home: Path) -> None:
    jobs.ensure_jobs_dir()
    broken = isolated_home / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(JobError, match="not valid JSON"):
        jobs.load(broken)
    partial = isolated_home / "partial.json"
    partial.write_text(json.dumps({"schema_version": 1, "name": "x"}))
    with pytest.raises(JobError, match="path_a"):
        jobs.load(partial)


def test_list_jobs_returns_loadable_jobs_and_reports_the_rest(isolated_home: Path) -> None:
    jobs.save(a_job(name="Zed"))
    jobs.save(a_job(name="Alpha"))
    (isolated_home / "bad.json").write_text("{")
    loaded, problems = jobs.list_jobs()
    assert [job.name for job in loaded] == ["Alpha", "Zed"]
    assert len(problems) == 1 and problems[0][0].name == "bad.json"


def test_list_jobs_on_a_missing_directory_is_empty_not_an_error(isolated_home: Path) -> None:
    assert jobs.list_jobs() == ([], [])


def test_find_by_name_accepts_the_name_or_the_slug(isolated_home: Path) -> None:
    jobs.save(a_job())
    assert jobs.find_by_name("Photos to NAS") is not None
    assert jobs.find_by_name("photos-to-nas") is not None
    assert jobs.find_by_name("nope") is None


def test_new_names_never_clobber_an_existing_job(isolated_home: Path) -> None:
    first = jobs.save(a_job())
    second = jobs.save(jobs.new_job("Photos to NAS", "/x", "/y"))
    assert first != second
    assert second.name == "photos-to-nas-2.json"


def test_duplicate_clears_history_and_snapshot(isolated_home: Path) -> None:
    original = a_job(mode="two_way", snapshot={"f.txt": {"kind": "file", "size": 1, "mtime_ns": 2}})
    original.last_run_utc = "2026-01-01T00:00:00Z"
    copy = jobs.duplicate(original, "Copy of it")
    assert copy.snapshot == {}
    assert copy.last_run_utc is None
    assert copy.path_a == original.path_a


def test_delete_removes_the_file(isolated_home: Path) -> None:
    job = a_job()
    path = jobs.save(job)
    jobs.delete(job)
    assert not path.exists()
    with pytest.raises(JobError):
        jobs.delete(jobs.new_job("never saved"))


def test_slugify_handles_awkward_names() -> None:
    assert jobs.slugify("  Photos → NAS (2024)!  ") == "photos-nas-2024"
    assert jobs.slugify("***") == "job"


def test_a_real_job_round_trips_its_snapshot(isolated_home: Path, tmp_path: Path) -> None:
    a = build(tmp_path / "a", {"f.txt": "x"})
    b = build(tmp_path / "b", {"f.txt": "x"})
    job = jobs.new_job("Two way", str(a), str(b))
    job.mode = "two_way"
    mtime_ns = os.stat(a / "f.txt").st_mtime_ns
    job.snapshot = {"f.txt": {"kind": "file", "size": 1, "mtime_ns": mtime_ns}}
    path = jobs.save(job)
    assert jobs.load(path).snapshot["f.txt"]["kind"] == "file"
