"""Worker thread, progress dialogs and the confirmation step.

The rule this module exists to keep: the worker thread never touches Tk. It pushes
messages onto a ``queue.Queue`` and the Tk main loop drains that queue on a timer, so
every widget call happens on the main thread and the window never blocks.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from ..core import jobs
from ..core.executor import RunEvent, execute
from ..core.models import (
    JobConfig,
    Plan,
    RunResult,
    human_bytes,
    human_duration,
    pause_summary,
)
from ..core.planner import build_snapshot, preview
from ..core.scanner import scan

POLL_MS = 100

Message = tuple[str, Any]
Work = Callable[["queue.Queue[Message]", threading.Event], None]


class TaskRunner:
    """Runs one callable on a background thread and delivers its messages to Tk."""

    def __init__(self, widget: tk.Misc, on_message: Callable[[Message], None]) -> None:
        self.widget = widget
        self.on_message = on_message
        self.queue: queue.Queue[Message] = queue.Queue()
        self.cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_id: str | None = None

    def start(self, work: Work) -> None:
        def target() -> None:
            try:
                work(self.queue, self.cancel)
            except Exception as exc:  # the thread must not die silently
                self.queue.put(("error", str(exc)))
            finally:
                self.queue.put(("finished", None))

        self._thread = threading.Thread(target=target, daemon=True, name="dirsynk-worker")
        self._thread.start()
        self._poll()

    def request_cancel(self) -> None:
        self.cancel.set()

    def stop_polling(self) -> None:
        if self._poll_id is not None:
            try:
                self.widget.after_cancel(self._poll_id)
            except tk.TclError:
                pass
            self._poll_id = None

    def _poll(self) -> None:
        try:
            while True:
                self.on_message(self.queue.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:  # the window went away mid-drain
            return
        self._poll_id = self.widget.after(POLL_MS, self._poll)


class PreviewDialog(tk.Toplevel):
    """Scans both sides and builds the plan, with a live count and a Cancel button."""

    def __init__(
        self,
        parent: tk.Misc,
        job: JobConfig,
        *,
        compare: str | None = None,
        on_done: Callable[[Plan | None, str | None], None],
    ) -> None:
        super().__init__(parent)
        self.title("Previewing…")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.on_done = on_done
        self.plan: Plan | None = None
        self.error: str | None = None

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"Scanning “{job.name}”", font=("TkDefaultFont", 12)).pack(anchor="w")
        self.count_label = ttk.Label(frame, text="scanned 0 items")
        self.count_label.pack(anchor="w", pady=(6, 10))
        self.bar = ttk.Progressbar(frame, mode="indeterminate", length=320)
        self.bar.pack(fill="x")
        self.bar.start(12)
        ttk.Button(frame, text="Cancel", command=self._cancel).pack(anchor="e", pady=(12, 0))

        self.runner = TaskRunner(self, self._handle)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.runner.start(_preview_work(job, compare))
        self.grab_set()

    def _cancel(self) -> None:
        self.runner.request_cancel()
        self.count_label.configure(text="cancelling…")

    def _handle(self, message: Message) -> None:
        kind, payload = message
        if kind == "scanned":
            self.count_label.configure(text=f"scanned {payload:,} items")
        elif kind == "plan":
            self.plan = payload
        elif kind == "error":
            self.error = payload
        elif kind == "finished":
            self.runner.stop_polling()
            self.grab_release()
            self.destroy()
            self.on_done(self.plan, self.error)


def _preview_work(job: JobConfig, compare: str | None) -> Work:
    def work(out: queue.Queue[Message], cancel: threading.Event) -> None:
        plan, _, _ = preview(
            job,
            cancel=cancel,
            progress=lambda n: out.put(("scanned", n)),
            compare=compare,  # type: ignore[arg-type]
        )
        if cancel.is_set():
            out.put(("error", "Preview cancelled — nothing was read further."))
        else:
            out.put(("plan", plan))

    return work


def confirm(parent: tk.Misc, plan: Plan, job: JobConfig) -> bool:
    """The explicit confirmation. Nothing is written before this returns True."""
    lines = [plan.summary_line(), ""]
    if plan.n_deletes:
        if job.deletion_policy == "quarantine":
            lines.append(
                f"{plan.n_deletes} item(s) ({human_bytes(plan.bytes_to_delete)}) will be "
                f"moved to .deleted inside the folder they are removed from."
            )
        else:
            lines.append(
                f"{plan.n_deletes} item(s) ({human_bytes(plan.bytes_to_delete)}) will be "
                f"PERMANENTLY DELETED. This cannot be undone."
            )
    else:
        lines.append("Nothing will be deleted.")
    if plan.n_conflicts:
        lines.append(
            f"{plan.n_conflicts} conflict(s) will be resolved in favour of the newer file."
        )
    lines.append("")
    lines.append("Proceed?")

    ask = messagebox.askokcancel if job.deletion_policy == "quarantine" else messagebox.askyesno
    return bool(
        ask(
            "Confirm sync",
            "\n".join(lines),
            icon=messagebox.WARNING if job.deletion_policy == "permanent" else messagebox.QUESTION,
            parent=parent,
        )
    )


class ExecuteDialog(tk.Toplevel):
    """Runs the plan: bytes for copies, count for items, a live log, and Cancel."""

    def __init__(
        self,
        parent: tk.Misc,
        plan: Plan,
        job: JobConfig,
        *,
        on_done: Callable[[RunResult | None, str | None], None],
    ) -> None:
        super().__init__(parent)
        self.title(f"Running “{job.name}”")
        self.transient(parent.winfo_toplevel())
        self.geometry("640x420")
        self.job = job
        self.result: RunResult | None = None
        self.error: str | None = None

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        self.item_label = ttk.Label(frame, text="Starting…", anchor="w")
        self.item_label.pack(fill="x")

        ttk.Label(frame, text="Bytes copied", anchor="w").pack(fill="x", pady=(10, 2))
        self.byte_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.byte_bar.pack(fill="x")
        self.byte_label = ttk.Label(frame, text="0 B of 0 B", anchor="w")
        self.byte_label.pack(fill="x")

        ttk.Label(frame, text="Items", anchor="w").pack(fill="x", pady=(10, 2))
        self.item_bar = ttk.Progressbar(frame, mode="determinate", maximum=100)
        self.item_bar.pack(fill="x")
        self.item_count_label = ttk.Label(frame, text="0 of 0", anchor="w")
        self.item_count_label.pack(fill="x")

        log_frame = ttk.Frame(frame)
        log_frame.pack(fill="both", expand=True, pady=(12, 8))
        self.log = tk.Text(log_frame, height=10, wrap="none", state="disabled")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.cancel_button = ttk.Button(frame, text="Cancel", command=self._cancel)
        self.cancel_button.pack(anchor="e")

        self.runner = TaskRunner(self, self._handle)
        self.on_done = on_done
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.runner.start(_execute_work(plan, job))
        self.grab_set()

    def _cancel(self) -> None:
        self.runner.request_cancel()
        self.cancel_button.configure(text="Cancelling…", state="disabled")

    def _append(self, line: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _handle(self, message: Message) -> None:
        kind, payload = message
        if kind == "event":
            self._handle_event(payload)
        elif kind == "error":
            self.error = payload
        elif kind == "result":
            self.result = payload
        elif kind == "finished":
            self.runner.stop_polling()
            self.grab_release()
            self.destroy()
            self.on_done(self.result, self.error)

    def _handle_event(self, event: RunEvent) -> None:
        if event.kind in ("item", "paused"):
            # A pause says so on the label: several silent seconds between files would
            # otherwise be indistinguishable from a stall.
            self.item_label.configure(text=event.message)
        if event.kind in ("item", "paused", "progress", "started", "done"):
            if event.total_bytes:
                self.byte_bar.configure(value=100 * event.done_bytes / event.total_bytes)
            self.byte_label.configure(
                text=f"{human_bytes(event.done_bytes)} of {human_bytes(event.total_bytes)}"
            )
            if event.total_items:
                self.item_bar.configure(value=100 * event.done_items / event.total_items)
            self.item_count_label.configure(text=f"{event.done_items} of {event.total_items}")
        if event.kind == "log":
            self._append(event.message)


def _execute_work(plan: Plan, job: JobConfig) -> Work:
    def work(out: queue.Queue[Message], cancel: threading.Event) -> None:
        result = execute(plan, job, cancel=cancel, on_event=lambda event: out.put(("event", event)))
        # Record the run, and in two-way mode remember what both sides now agree on.
        job.last_run_utc = jobs.utc_now()
        job.last_result = result.to_json()
        if job.mode == "two_way" and not result.errors and not result.cancelled:
            scan_a = scan(job.root_a, exclude=job.exclude, follow_symlinks=job.follow_symlinks)
            scan_b = scan(job.root_b, exclude=job.exclude, follow_symlinks=job.follow_symlinks)
            if not scan_a.errors and not scan_b.errors:
                job.snapshot = build_snapshot(scan_a.entries, scan_b.entries)
        if job.file_path is not None:
            jobs.save(job)
        out.put(("result", result))

    return work


class SummaryDialog(tk.Toplevel):
    """What happened, including every per-item failure, with the log saveable."""

    def __init__(self, parent: tk.Misc, result: RunResult, job: JobConfig) -> None:
        super().__init__(parent)
        self.title("Sync finished")
        self.transient(parent.winfo_toplevel())
        self.geometry("620x360")
        self.result = result
        self.job = job

        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        headline = "Cancelled part-way" if result.cancelled else "Finished"
        ttk.Label(frame, text=headline, font=("TkDefaultFont", 14, "bold")).pack(anchor="w")
        ttk.Label(
            frame,
            text=(
                f"{result.copied} copied ({human_bytes(result.bytes_copied)}) · "
                f"{result.created} folders created · {result.deleted} deleted · "
                f"{result.skipped} skipped · {result.failed} failed"
            ),
        ).pack(anchor="w", pady=(6, 0))
        ttk.Label(frame, text=f"Elapsed: {human_duration(result.elapsed_s)}").pack(anchor="w")
        if result.log_path:
            self.log_label = ttk.Label(frame, text=f"Log: {result.log_path}")
            self.log_label.pack(anchor="w")
        if result.log_error:
            ttk.Label(frame, text=result.log_error, foreground="#b00020").pack(anchor="w")

        if result.errors:
            ttk.Label(frame, text=f"{len(result.errors)} item(s) failed:").pack(
                anchor="w", pady=(12, 4)
            )
            tree = ttk.Treeview(frame, columns=("path", "what", "why"), show="headings", height=8)
            for column, heading, width in (
                ("path", "Path", 240),
                ("what", "Action", 100),
                ("why", "Error", 220),
            ):
                tree.heading(column, text=heading)
                tree.column(column, width=width, anchor="w")
            for error in result.errors:
                tree.insert("", "end", values=(error.rel, error.action, error.message))
            tree.pack(fill="both", expand=True)
        else:
            ttk.Label(frame, text="No errors.").pack(anchor="w", pady=(12, 4))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Save log…", command=self._save_log).pack(side="left")
        ttk.Button(buttons, text="Close", command=self.destroy).pack(side="right")

    def _log_text(self) -> str:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [
            f"dirsynk run — {self.job.name} — {stamp}",
            f"A: {self.job.root_a}",
            f"B: {self.job.root_b}",
            f"Mode: {self.job.mode} · Compare: {self.job.compare} · "
            f"Deletions: {self.job.deletion_policy}",
            "",
            self.result.summary_line(),
        ]
        pause = pause_summary(self.job.copy_pause_s, self.result.copied)
        if pause:
            lines.insert(4, f"Paused {pause}")
        if self.result.log_path:
            lines.append(f"Full operation log: {self.result.log_path}")
        if self.result.errors:
            lines.append("")
            lines.append("Failures:")
            lines.extend(
                f"  {error.rel} [{error.action}]: {error.message}" for error in self.result.errors
            )
        return "\n".join(lines) + "\n"

    def _save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Save log",
            defaultextension=".txt",
            initialfile=f"dirsynk-{jobs.slugify(self.job.name)}.txt",
        )
        if not path:
            return
        try:
            Path(path).write_text(self._log_text(), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("Could not save log", str(exc), parent=self)
