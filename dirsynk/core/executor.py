"""Apply a Plan, one item at a time, reporting as it goes.

Two rules shape this module. A per-item failure never aborts the run: it is caught,
recorded against the item, and the next item is attempted. And nothing is ever written
in place — a copy lands on a ``.dirsynk.tmp`` sibling and is moved onto the target with
``os.replace``, so an interrupted copy cannot leave a truncated file behind.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from . import deleter, runlog
from .models import TEMP_SUFFIX, Action, ItemError, JobConfig, Plan, RunResult
from .runlog import COPY, CREATE_DIR, DELETE, RunLog

COPY_CHUNK = 1 << 20

EventKind = Literal["started", "item", "progress", "log", "paused", "done"]


@dataclass(frozen=True)
class RunEvent:
    """What the worker thread reports. The UI turns these into queue messages."""

    kind: EventKind
    message: str = ""
    rel: str = ""
    done_items: int = 0
    total_items: int = 0
    done_bytes: int = 0
    total_bytes: int = 0
    result: RunResult | None = None


EventSink = Callable[[RunEvent], None]


def ordered_actions(actions: Iterable[Action]) -> list[Action]:
    """mkdir (parents first) → copies → file deletes → directory deletes (deepest first)."""
    included = [a for a in actions if a.included]
    mkdirs = sorted(
        (a for a in included if a.kind == "mkdir"), key=lambda a: (len(a.rel.parts), str(a.rel))
    )
    copies = sorted(
        (a for a in included if a.kind in ("copy_new", "copy_update", "conflict")),
        key=lambda a: (len(a.rel.parts), str(a.rel)),
    )
    file_deletes = sorted(
        (a for a in included if a.kind == "delete_file"), key=lambda a: str(a.rel)
    )
    dir_deletes = sorted(
        (a for a in included if a.kind == "delete_dir"),
        key=lambda a: (len(a.rel.parts), str(a.rel)),
        reverse=True,
    )
    skips = [a for a in included if a.kind == "skip"]
    return mkdirs + copies + file_deletes + dir_deletes + skips


def _copy_file(
    src: Path,
    dst: Path,
    *,
    cancel: threading.Event | None,
    on_bytes: Callable[[int], None] | None = None,
) -> bool:
    """Copy one file through a temp sibling. Returns False if cancelled part-way.

    This is ``shutil.copy2`` in effect — the metadata copy is the same — done in chunks so
    a long copy can report progress and stop when asked.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + TEMP_SUFFIX)
    try:
        if src.is_symlink():
            if tmp.exists() or tmp.is_symlink():
                os.remove(tmp)
            os.symlink(os.readlink(src), tmp)
        else:
            with open(src, "rb") as reader, open(tmp, "wb") as writer:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise _Cancelled
                    chunk = reader.read(COPY_CHUNK)
                    if not chunk:
                        break
                    writer.write(chunk)
                    if on_bytes is not None:
                        on_bytes(len(chunk))
            shutil.copystat(src, dst=tmp, follow_symlinks=True)
        os.replace(tmp, dst)
        return True
    except _Cancelled:
        _discard(tmp)
        return False
    except BaseException:
        _discard(tmp)
        raise


class _Cancelled(Exception):
    pass


def _wait(seconds: float, cancel: threading.Event | None) -> bool:
    """Hold for ``seconds``. Returns False if Cancel was pressed during the wait.

    Waiting on the event rather than sleeping is what keeps Cancel responsive: a run
    pausing a minute between files would otherwise ignore the button for a minute.
    """
    if cancel is None:
        time.sleep(seconds)
        return True
    return not cancel.wait(seconds)


def _discard(tmp: Path) -> None:
    try:
        if tmp.is_symlink() or tmp.exists():
            os.remove(tmp)
    except OSError:
        pass


def _root_written_to(action: Action, config: JobConfig) -> Path:
    return config.root_b if action.direction == "a_to_b" else config.root_a


def _delete_destination(action: Action, config: JobConfig, stamp: str) -> Path | None:
    """Where a delete puts the item: its place in ``.deleted``, or nowhere at all."""
    if config.deletion_policy != "quarantine":
        return None
    return deleter.quarantine_target(
        _root_written_to(action, config), PurePosixPath(action.rel), stamp
    )


def _log_header(config: JobConfig, plan: Plan, pause: float) -> list[str]:
    started = datetime.now().strftime(runlog.TIME_FORMAT)
    header = [
        f"dirsynk run — {config.name} — started {started}",
        f"A: {config.root_a}",
        f"B: {config.root_b}",
        f"Mode: {config.mode} · Compare: {plan.compare} · Deletions: {config.deletion_policy}",
    ]
    if pause:
        header.append(f"Pausing {pause:g}s between file copies")
    return header


def execute(
    plan: Plan,
    config: JobConfig,
    *,
    cancel: threading.Event | None = None,
    on_event: EventSink | None = None,
    stamp: str | None = None,
    pause_s: float | None = None,
) -> RunResult:
    """Perform every ticked action in the plan and report what happened.

    ``pause_s`` overrides the job's own pause between file copies for this run only —
    the CLI's ``--pause`` — and is never written back to the job.
    """
    emit = on_event or (lambda event: None)
    actions = ordered_actions(plan.actions)
    pause = max(0.0, config.copy_pause_s if pause_s is None else pause_s)
    copies_done = 0  # a pause goes *between* copies, so the first one never waits
    result = RunResult()
    started = time.monotonic()
    stamp = stamp or deleter.run_stamp()
    total_items = len(actions)
    total_bytes = sum(a.size for a in actions if a.is_copy)
    done_bytes = 0

    log = RunLog(runlog.log_path(config, stamp))
    log.open(_log_header(config, plan, pause))
    result.log_path = str(log.path)

    emit(RunEvent("started", total_items=total_items, total_bytes=total_bytes))

    for index, action in enumerate(actions, start=1):
        if cancel is not None and cancel.is_set():
            result.cancelled = True
            emit(RunEvent("log", message="Cancelled."))
            break

        emit(
            RunEvent(
                "item",
                rel=str(action.rel),
                message=f"{action.kind}: {action.rel}",
                done_items=index - 1,
                total_items=total_items,
                done_bytes=done_bytes,
                total_bytes=total_bytes,
            )
        )

        def report_bytes(n: int, _index: int = index) -> None:
            nonlocal done_bytes
            done_bytes += n
            emit(
                RunEvent(
                    "progress",
                    done_items=_index - 1,
                    total_items=total_items,
                    done_bytes=done_bytes,
                    total_bytes=total_bytes,
                )
            )

        try:
            if action.kind == "skip":
                result.skipped += 1
            elif action.kind == "mkdir":
                assert action.dst is not None
                log.begin(CREATE_DIR, action.src, action.dst)
                action.dst.mkdir(parents=True, exist_ok=True)
                result.created += 1
                log.success()
            elif action.is_copy:
                assert action.src is not None and action.dst is not None
                _clear_the_way(action, config, stamp, log)
                if action.src.is_dir() and not action.src.is_symlink():
                    # A conflict the directory side won: make the directory, don't copy it.
                    log.begin(CREATE_DIR, action.src, action.dst)
                    action.dst.mkdir(parents=True, exist_ok=True)
                    result.created += 1
                    log.success()
                    emit(RunEvent("log", message=f"mkdir: {action.rel}", rel=str(action.rel)))
                    continue
                if pause and copies_done:
                    emit(
                        RunEvent(
                            "paused",
                            message=f"Pausing {pause:g}s before the next copy…",
                            rel=str(action.rel),
                            done_items=index - 1,
                            total_items=total_items,
                            done_bytes=done_bytes,
                            total_bytes=total_bytes,
                        )
                    )
                    if not _wait(pause, cancel):
                        result.cancelled = True
                        emit(RunEvent("log", message="Cancelled."))
                        break
                before = done_bytes
                log.begin(COPY, action.src, action.dst)
                if _copy_file(action.src, action.dst, cancel=cancel, on_bytes=report_bytes):
                    log.success()
                    copies_done += 1
                    result.copied += 1
                    result.bytes_copied += action.size
                else:
                    log.cancelled()
                    done_bytes = before
                    result.cancelled = True
                    emit(RunEvent("log", message="Cancelled."))
                    break
            elif action.is_delete:
                assert action.dst is not None
                if not (action.dst.exists() or action.dst.is_symlink()):
                    # Already gone — a resolved conflict took it, or something else did.
                    result.skipped += 1
                    emit(
                        RunEvent(
                            "log",
                            message=f"already gone: {action.rel}",
                            rel=str(action.rel),
                        )
                    )
                    continue
                # The item removed is the source of a delete; where quarantine puts
                # it is the destination. Permanent deletion has no destination at all.
                log.begin(DELETE, action.dst, _delete_destination(action, config, stamp))
                deleter.remove(
                    action.dst,
                    rel=action.rel,
                    root=_root_written_to(action, config),
                    policy=config.deletion_policy,
                    stamp=stamp,
                    is_dir=action.kind == "delete_dir",
                )
                result.deleted += 1
                log.success()
        except OSError as exc:
            # A failure here costs this one item and nothing else.
            result.failed += 1
            message = exc.strerror or str(exc)
            log.failure(message)
            result.errors.append(ItemError(str(action.rel), action.kind, message))
            emit(RunEvent("log", message=f"FAILED {action.rel}: {message}", rel=str(action.rel)))
        else:
            emit(RunEvent("log", message=f"{action.kind}: {action.rel}", rel=str(action.rel)))

    log.close()
    if log.error:
        # The sync itself was fine; only the record of it was not.
        result.log_error = log.error
        emit(RunEvent("log", message=log.error))
    result.elapsed_s = time.monotonic() - started
    emit(
        RunEvent(
            "done",
            done_items=total_items,
            total_items=total_items,
            done_bytes=done_bytes,
            total_bytes=total_bytes,
            result=result,
        )
    )
    return result


def _clear_the_way(action: Action, config: JobConfig, stamp: str, log: RunLog) -> None:
    """A file cannot be written over a directory: resolve a kind clash under the policy.

    This is a delete like any other and gets its own line, written before the copy's —
    the copy has not begun a line yet, so the two never interleave.
    """
    dst = action.dst
    src = action.src
    if dst is None or src is None:
        return
    if not (dst.exists() or dst.is_symlink()):
        return
    dst_is_dir = dst.is_dir() and not dst.is_symlink()
    src_is_dir = src.is_dir() and not src.is_symlink()
    if dst_is_dir == src_is_dir:
        return
    log.begin(DELETE, dst, _delete_destination(action, config, stamp))
    deleter.remove_tree(
        dst,
        rel=PurePosixPath(action.rel),
        root=_root_written_to(action, config),
        policy=config.deletion_policy,
        stamp=stamp,
    )
    log.success()
