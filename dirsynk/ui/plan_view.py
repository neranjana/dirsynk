"""The plan window: every copy and delete, grouped, filterable, and tickable.

Nothing here writes to disk. The Run button is the only way out of this window into the
executor, and it goes through an explicit confirmation first.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import messagebox, ttk

from ..core.models import (
    ACTION_ORDER,
    Action,
    JobConfig,
    Plan,
    RunResult,
    human_bytes,
    pause_summary,
)
from .runner import ExecuteDialog, SummaryDialog, confirm

TICKED = "☑"
UNTICKED = "☐"

KIND_LABELS: dict[str, str] = {
    "mkdir": "Create folder",
    "copy_new": "Copy (new)",
    "copy_update": "Copy (update)",
    "conflict": "Conflict",
    "delete_file": "Delete file",
    "delete_dir": "Delete folder",
    "skip": "Skip",
}

COLUMNS = (
    ("include", "", 40),
    ("action", "Action", 120),
    ("side", "Side", 70),
    ("path", "Path", 320),
    ("type", "Type", 60),
    ("size", "Size", 90),
    ("reason", "Reason", 300),
)


class PlanWindow(tk.Toplevel):
    """Shows a plan and, only on an explicit confirmation, runs it."""

    def __init__(
        self,
        parent: tk.Misc,
        job: JobConfig,
        plan: Plan,
        *,
        on_repreview: Callable[[], None],
        on_finished: Callable[[RunResult], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.title(f"Plan — {job.name}")
        self.geometry("1080x620")
        self.job = job
        self.plan = plan
        self.on_repreview = on_repreview
        self.on_finished = on_finished
        self.sort_column: str | None = None
        self.sort_reverse = False
        self.row_action: dict[str, int] = {}

        self._build_header()
        self._build_tree()
        self._build_footer()
        self.refresh()

    # --- layout ---------------------------------------------------------------

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(12, 10, 12, 4))
        header.pack(fill="x")

        mode = "Mirror (A → B)" if self.job.mode == "mirror" else "Two-way"
        ttk.Label(
            header, text=f"{self.job.name} — {mode}", font=("TkDefaultFont", 13, "bold")
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=self.plan.compared_by()).grid(
            row=0, column=1, sticky="e", padx=(12, 0)
        )
        header.columnconfigure(0, weight=1)

        ttk.Label(header, text=f"A: {self.job.root_a}    B: {self.job.root_b}").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 0)
        )

        # The pause is worth seeing before you commit to a run, not after: it is the
        # difference between a two-minute job and an afternoon.
        pause = pause_summary(self.job.copy_pause_s, self.plan.n_copies)
        if pause:
            self.pause_label = ttk.Label(header, text=f"Pausing {pause}", foreground="#8a6d00")
            self.pause_label.grid(row=1, column=1, sticky="e", padx=(12, 0))

        if self.plan.first_run:
            ttk.Label(
                header,
                text=(
                    "First two-way run: there is no snapshot yet, so everything is copied "
                    "both ways and nothing is deleted."
                ),
                foreground="#8a6d00",
            ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))

        if self.plan.errors:
            ttk.Label(
                header,
                text=f"{len(self.plan.errors)} path(s) could not be scanned — see Details",
                foreground="#b00020",
            ).grid(row=3, column=0, sticky="w", pady=(4, 0))
            ttk.Button(header, text="Details…", command=self._show_scan_errors).grid(
                row=3, column=1, sticky="e"
            )

        filter_row = ttk.Frame(self, padding=(12, 6, 12, 4))
        filter_row.pack(fill="x")
        ttk.Label(filter_row, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh())
        entry = ttk.Entry(filter_row, textvariable=self.filter_var, width=40)
        entry.pack(side="left", padx=(6, 12))
        ttk.Button(filter_row, text="Tick all", command=lambda: self._set_all(True)).pack(
            side="left"
        )
        ttk.Button(filter_row, text="Untick all", command=lambda: self._set_all(False)).pack(
            side="left", padx=(6, 0)
        )
        ttk.Label(
            filter_row, text="Click a tick to leave a row out of this run", foreground="#666666"
        ).pack(side="left", padx=(12, 0))

    def _build_tree(self) -> None:
        container = ttk.Frame(self, padding=(12, 4))
        container.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(
            container,
            columns=[name for name, _, _ in COLUMNS],
            show="tree headings",
            selectmode="browse",
        )
        self.tree.column("#0", width=200, stretch=False)
        self.tree.heading("#0", text="Grouped by action")
        for name, heading, width in COLUMNS:
            self.tree.heading(name, text=heading, command=lambda c=name: self._sort_by(c))
            self.tree.column(name, width=width, anchor="e" if name == "size" else "w")

        vertical = ttk.Scrollbar(container, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        container.rowconfigure(0, weight=1)
        container.columnconfigure(0, weight=1)

        self.tree.tag_configure("conflict", background="#fff3cd", foreground="#7a5b00")
        self.tree.tag_configure("delete", foreground="#b00020")
        self.tree.tag_configure("excluded", foreground="#9a9a9a")
        self.tree.tag_configure("group", font=("TkDefaultFont", 11, "bold"))

        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda _event: self._toggle(self.tree.focus()))

    def _build_footer(self) -> None:
        footer = ttk.Frame(self, padding=(12, 6, 12, 12))
        footer.pack(fill="x")
        self.summary_label = ttk.Label(footer, text="", font=("TkDefaultFont", 12, "bold"))
        self.summary_label.pack(side="left")

        ttk.Button(footer, text="Close", command=self.destroy).pack(side="right")
        self.run_button = ttk.Button(footer, text="Run…", command=self._run)
        self.run_button.pack(side="right", padx=(0, 8))
        ttk.Button(footer, text="Re-preview", command=self._repreview).pack(
            side="right", padx=(0, 8)
        )

    # --- contents -------------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the rows from the plan, honouring the filter and the sort."""
        self.tree.delete(*self.tree.get_children())
        self.row_action.clear()
        needle = self.filter_var.get().strip().lower()

        by_kind: dict[str, list[tuple[int, Action]]] = {}
        for index, action in enumerate(self.plan.actions):
            if needle and needle not in f"{action.rel} {action.reason}".lower():
                continue
            by_kind.setdefault(action.kind, []).append((index, action))

        for kind in sorted(by_kind, key=lambda k: ACTION_ORDER[k]):
            rows = by_kind[kind]
            group = self.tree.insert(
                "", "end", text=f"{KIND_LABELS[kind]} ({len(rows)})", open=True, tags=("group",)
            )
            for index, action in self._sorted(rows):
                iid = self.tree.insert(
                    group, "end", values=self._values(action), tags=self._tags(action)
                )
                self.row_action[iid] = index

        self._update_summary()

    def _sorted(self, rows: list[tuple[int, Action]]) -> list[tuple[int, Action]]:
        if self.sort_column is None:
            return sorted(rows, key=lambda pair: str(pair[1].rel))
        keys: dict[str, Callable[[Action], object]] = {
            "include": lambda a: not a.included,
            "action": lambda a: a.kind,
            "side": lambda a: a.side,
            "path": lambda a: str(a.rel),
            "type": lambda a: _type_of(a),
            "size": lambda a: a.size,
            "reason": lambda a: a.reason,
        }
        key = keys.get(self.sort_column, lambda a: str(a.rel))
        return sorted(rows, key=lambda pair: key(pair[1]), reverse=self.sort_reverse)  # type: ignore[arg-type,return-value]

    def _values(self, action: Action) -> tuple[str, ...]:
        return (
            TICKED if action.included else UNTICKED,
            KIND_LABELS[action.kind],
            action.side,
            str(action.rel),
            _type_of(action),
            human_bytes(action.size) if action.size else "",
            action.reason,
        )

    def _tags(self, action: Action) -> tuple[str, ...]:
        tags: list[str] = []
        if action.kind == "conflict":
            tags.append("conflict")
        elif action.is_delete:
            tags.append("delete")
        if not action.included:
            tags.append("excluded")
        return tuple(tags)

    def _update_summary(self) -> None:
        self.summary_label.configure(text=self.plan.summary_line())
        self.run_button.configure(state="normal" if self.plan.has_work else "disabled")

    # --- interaction ----------------------------------------------------------

    def _on_click(self, event: tk.Event) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#1":
            return
        self._toggle(self.tree.identify_row(event.y))

    def _toggle(self, iid: str) -> None:
        index = self.row_action.get(iid)
        if index is None:
            return
        action = self.plan.actions[index]
        self.plan.set_included(index, not action.included)
        updated = self.plan.actions[index]
        self.tree.item(iid, values=self._values(updated), tags=self._tags(updated))
        self._update_summary()

    def _set_all(self, included: bool) -> None:
        for index in sorted(self.row_action.values()):
            self.plan.set_included(index, included)
        self.refresh()

    def _sort_by(self, column: str) -> None:
        self.sort_reverse = not self.sort_reverse if self.sort_column == column else False
        self.sort_column = column
        self.refresh()

    def _show_scan_errors(self) -> None:
        messagebox.showwarning(
            "Paths that could not be scanned",
            "\n".join(self.plan.errors[:40]),
            parent=self,
        )

    def _repreview(self) -> None:
        self.destroy()
        self.on_repreview()

    def _run(self) -> None:
        if self.plan.config_fingerprint != self.job.fingerprint():
            messagebox.showwarning(
                "Settings changed",
                "The job's settings changed after this plan was made, so it no longer "
                "describes what would happen. Re-preview before running.",
                parent=self,
            )
            return
        if not self.plan.has_work:
            messagebox.showinfo("Nothing to do", "No rows are ticked.", parent=self)
            return
        if not confirm(self, self.plan, self.job):
            return
        ExecuteDialog(self, self.plan, self.job, on_done=self._finished)

    def _finished(self, result: RunResult | None, error: str | None) -> None:
        if error:
            messagebox.showerror("Run failed", error, parent=self)
            return
        if result is None:
            return
        SummaryDialog(self.master, result, self.job)
        if self.on_finished is not None:
            self.on_finished(result)
        # The plan describes a world that no longer exists.
        self.destroy()


def _type_of(action: Action) -> str:
    return "folder" if action.kind in ("mkdir", "delete_dir") else "file"
