"""Load, save and list job files under ``~/.dirsynk``.

Writes are atomic — a temp file in the same directory, then ``os.replace`` — so a job
file is never left half-written. Anything unrecognised in a file is rejected with a
message that names the problem, rather than guessed at.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import (
    COMPARE_MODES,
    COPY_PAUSE_LIMIT_S,
    DEFAULT_EXCLUDES,
    DELETION_POLICIES,
    SCHEMA_VERSION,
    SYNC_MODES,
    JobConfig,
)

#: Override for tests and for anyone who keeps their jobs elsewhere.
HOME_ENV_VAR = "DIRSYNK_HOME"


class JobError(Exception):
    """A job file could not be read, understood, or written."""


def jobs_dir() -> Path:
    override = os.environ.get(HOME_ENV_VAR)
    return Path(override) if override else Path.home() / ".dirsynk"


def ensure_jobs_dir() -> Path:
    directory = jobs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "job"


def job_path(name: str) -> Path:
    return jobs_dir() / f"{slugify(name)}.json"


def unique_job_path(name: str) -> Path:
    """A path for a new job that will not clobber an existing one."""
    path = job_path(name)
    counter = 2
    while path.exists():
        path = jobs_dir() / f"{slugify(name)}-{counter}.json"
        counter += 1
    return path


def new_job(name: str, path_a: str = "", path_b: str = "") -> JobConfig:
    return JobConfig(
        name=name,
        path_a=path_a,
        path_b=path_b,
        exclude=list(DEFAULT_EXCLUDES),
        created_utc=utc_now(),
    )


def save(job: JobConfig, *, path: Path | None = None) -> Path:
    """Write the job atomically and remember where it went."""
    ensure_jobs_dir()
    target = path or job.file_path or unique_job_path(job.name)
    temp = target.with_name(target.name + ".tmp")
    payload = json.dumps(job.to_json(), indent=2, ensure_ascii=False)
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise JobError(f"Could not write {target}: {exc.strerror or exc}") from exc
    job.file_path = target
    return target


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    if key not in data:
        raise JobError(f"{path.name}: missing required field '{key}'.")
    return data[key]


def _copy_pause(raw: Any, path: Path) -> float:
    """The pause between copies, or a JobError naming what is wrong with it."""
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        raise JobError(f"{path.name}: 'copy_pause_s' must be a number of seconds.")
    if not 0 <= raw <= COPY_PAUSE_LIMIT_S:
        raise JobError(
            f"{path.name}: 'copy_pause_s' is {raw!r}, which is outside "
            f"0–{COPY_PAUSE_LIMIT_S:g} seconds."
        )
    return float(raw)


def load(path: Path) -> JobConfig:
    """Read one job file, rejecting anything it cannot honour exactly."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise JobError(f"Could not read {path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JobError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JobError(f"{path.name} does not contain a job object.")

    version = data.get("schema_version", SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise JobError(
            f"{path.name} has schema_version {version!r}, but this version of dirsynk "
            f"only understands {SCHEMA_VERSION}. Refusing to guess."
        )

    compare = data.get("compare", "size_mtime")
    if compare not in COMPARE_MODES:
        raise JobError(
            f"{path.name} has compare {compare!r}, which is not one of "
            f"{', '.join(COMPARE_MODES)}. Refusing to guess."
        )
    mode = data.get("mode", "mirror")
    if mode not in SYNC_MODES:
        raise JobError(
            f"{path.name} has mode {mode!r}, which is not one of {', '.join(SYNC_MODES)}."
        )
    policy = data.get("deletion_policy", "quarantine")
    if policy not in DELETION_POLICIES:
        raise JobError(
            f"{path.name} has deletion_policy {policy!r}, which is not one of "
            f"{', '.join(DELETION_POLICIES)}."
        )

    snapshot = data.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        raise JobError(f"{path.name}: 'snapshot' must be an object.")
    exclude = data.get("exclude", list(DEFAULT_EXCLUDES))
    if not isinstance(exclude, list) or any(not isinstance(item, str) for item in exclude):
        raise JobError(f"{path.name}: 'exclude' must be a list of strings.")
    pause = _copy_pause(data.get("copy_pause_s", 0), path)

    return JobConfig(
        name=str(_require(data, "name", path)),
        path_a=str(_require(data, "path_a", path)),
        path_b=str(_require(data, "path_b", path)),
        mode=mode,
        deletion_policy=policy,
        compare=compare,
        # Kept even while unused, so switching criterion away and back loses nothing.
        mtime_tolerance_s=float(data.get("mtime_tolerance_s", 2)),
        follow_symlinks=bool(data.get("follow_symlinks", False)),
        copy_pause_s=pause,
        exclude=list(exclude),
        created_utc=str(data.get("created_utc") or utc_now()),
        last_run_utc=data.get("last_run_utc"),
        last_result=data.get("last_result"),
        snapshot=snapshot,
        file_path=path,
    )


def list_jobs() -> tuple[list[JobConfig], list[tuple[Path, str]]]:
    """Every job in the jobs directory, plus the ones that would not load and why."""
    directory = jobs_dir()
    loaded: list[JobConfig] = []
    problems: list[tuple[Path, str]] = []
    if not directory.is_dir():
        return loaded, problems
    for path in sorted(directory.glob("*.json")):
        try:
            loaded.append(load(path))
        except JobError as exc:
            problems.append((path, str(exc)))
    loaded.sort(key=lambda job: job.name.lower())
    return loaded, problems


def find_by_name(name: str) -> JobConfig | None:
    """Look a job up the way the CLI's ``--job`` does: by name, then by slug."""
    loaded, _ = list_jobs()
    for job in loaded:
        if job.name == name:
            return job
    wanted = slugify(name)
    for job in loaded:
        if slugify(job.name) == wanted or (job.file_path and job.file_path.stem == wanted):
            return job
    return None


def delete(job: JobConfig) -> None:
    if job.file_path is None:
        raise JobError(f"Job {job.name!r} has never been saved.")
    try:
        job.file_path.unlink()
    except OSError as exc:
        raise JobError(f"Could not delete {job.file_path}: {exc.strerror or exc}") from exc


def duplicate(job: JobConfig, new_name: str) -> JobConfig:
    """A copy of a job under a new name, with its run history and snapshot cleared."""
    copy = JobConfig(
        name=new_name,
        path_a=job.path_a,
        path_b=job.path_b,
        mode=job.mode,
        deletion_policy=job.deletion_policy,
        compare=job.compare,
        mtime_tolerance_s=job.mtime_tolerance_s,
        follow_symlinks=job.follow_symlinks,
        copy_pause_s=job.copy_pause_s,
        exclude=list(job.exclude),
        created_utc=utc_now(),
    )
    return copy
