"""The per-run log file: one line per thing a run does to the disk.

Every run writes one, alongside the job files in ``~/.dirsynk``, named after the job
file and the moment the run started — ``photos-to-nas-20260905-142530.log``, the same
stamp the run's ``.deleted`` folder carries, so a log and its quarantined files name
each other.

Each line is written in two halves. The operation, its source and its destination go
down *before* the work starts and are flushed immediately; the outcome is appended to
that same line once the work finishes. A run killed mid-copy therefore leaves one line
with no status on the end, naming exactly the operation that was in flight — the one
thing a log assembled after the fact could never tell you.

Nothing in here may break a sync. A log that cannot be written records why it gave up
and gets out of the way; the files still get copied.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from . import jobs
from .models import JobConfig

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

#: The words the log uses for what it is doing. Plan vocabulary is for the plan; a log
#: reader wants the verb, not the reason the planner chose it.
COPY = "copy"
CREATE_DIR = "create dir"
DELETE = "delete"

#: Widest of the three, so the paths line up in a column.
_OPERATION_WIDTH = len(CREATE_DIR)


def log_path(config: JobConfig, stamp: str) -> Path:
    """``~/.dirsynk/<job file name>-<run start>.log``.

    A job that has never been saved has no file name to borrow, so it uses the slug its
    file *would* get.
    """
    stem = config.file_path.stem if config.file_path is not None else jobs.slugify(config.name)
    return jobs.jobs_dir() / f"{stem}-{stamp}.log"


def _shown(path: Path | None) -> str:
    return str(path) if path is not None else "-"


class RunLog:
    """Appends a line per operation, and never lets its own failure reach the run."""

    def __init__(self, path: Path) -> None:
        self.path = path
        #: Why logging stopped, if it did. The run carries on regardless.
        self.error: str | None = None
        self.operations = 0
        self.succeeded = 0
        self.failed = 0
        self.unfinished = 0
        self._handle = None
        self._line_open = False

    # --- the file ---------------------------------------------------------------

    def open(self, header: Iterable[str] = ()) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Appending, not truncating: two runs of one job started in the same second
            # would otherwise lose one of the two logs.
            self._handle = open(self.path, "a", encoding="utf-8")
        except OSError as exc:
            self._give_up(exc)
            return
        self._write("".join(f"# {line}\n" for line in header))

    def close(self) -> None:
        """Finish any open line, write the summary, and let the file go."""
        self._end("interrupted")
        self._write(f"# finished {datetime.now().strftime(TIME_FORMAT)} — {self.summary_line()}\n")
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError as exc:  # pragma: no cover - closing a file that already broke
                self.error = exc.strerror or str(exc)

    # --- one operation ----------------------------------------------------------

    def begin(self, operation: str, src: Path | None, dst: Path | None) -> None:
        """Write the half-line for an operation that is about to be attempted."""
        self.operations += 1
        self._line_open = True
        self._write(
            f"{datetime.now().strftime(TIME_FORMAT)}  {operation:<{_OPERATION_WIDTH}}  "
            f"{_shown(src)}  ->  {_shown(dst)}"
        )

    def success(self) -> None:
        if self._end("success"):
            self.succeeded += 1

    def failure(self, message: str) -> None:
        if self._end(f"failed: {message}"):
            self.failed += 1

    def cancelled(self) -> None:
        """Stopped part-way by the user: attempted, but neither done nor failed."""
        if self._end("cancelled"):
            self.unfinished += 1

    def _end(self, status: str) -> bool:
        """Append the outcome to the open line. False if there was no open line."""
        if not self._line_open:
            return False
        self._line_open = False
        self._write(f"  {status}\n")
        return True

    # --- the tally --------------------------------------------------------------

    def summary_line(self) -> str:
        parts = [
            f"{self.operations} operations",
            f"{self.succeeded} succeeded",
            f"{self.failed} failed",
        ]
        if self.unfinished:
            parts.append(f"{self.unfinished} not finished")
        return ", ".join(parts)

    # --- plumbing ---------------------------------------------------------------

    def _write(self, text: str) -> None:
        if self._handle is None:
            return
        try:
            self._handle.write(text)
            # Flushed every time: the point of writing the operation before doing it is
            # that it survives whatever the operation does not.
            self._handle.flush()
        except OSError as exc:
            self._give_up(exc)

    def _give_up(self, exc: OSError) -> None:
        self.error = f"could not write {self.path}: {exc.strerror or exc}"
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
