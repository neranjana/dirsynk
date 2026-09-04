"""The job editor: two folders, a mode, a criterion, a deletion policy, exclusions."""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from tkinter import filedialog, messagebox, simpledialog, ttk

from ..core import jobs
from ..core.jobs import JobError
from ..core.models import JobConfig
from ..core.validate import path_problem, validate
from .plan_view import PlanWindow
from .runner import PreviewDialog

COMPARE_CHOICES: tuple[tuple[str, str, str], ...] = (
    (
        "size_mtime",
        "Size and timestamp (recommended)",
        "files differ if their size or modified time differs",
    ),
    (
        "size_only",
        "Size only",
        "ignores timestamps; a same-size edit will not be detected",
    ),
    (
        "content",
        "Size and contents (SHA-256)",
        "exact, slowest; only hashes when sizes match",
    ),
)

MODE_CHOICES: tuple[tuple[str, str, str], ...] = (
    ("mirror", "Mirror (A → B)", "A is the source of truth; B is made to match it"),
    ("two_way", "Two-way", "changes on either side travel to the other"),
)

POLICY_CHOICES: tuple[tuple[str, str, str], ...] = (
    (
        "quarantine",
        "Move to .deleted (recommended)",
        "kept under <folder>/.deleted/<timestamp>/ until you remove them yourself",
    ),
    ("permanent", "Delete permanently", "removed outright; this cannot be undone"),
)

RED = "#b00020"
GREY = "#666666"


class ScrollableArea(ttk.Frame):
    """A frame whose contents scroll vertically when they outgrow the window."""

    def __init__(self, parent: tk.Misc) -> None:
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.content = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        # Each Configure handler reconfigures the other widget, so both skip a value they
        # have already applied and the resizing settles instead of ringing.
        self._applied_width = -1
        self._applied_region: tuple[int, int, int, int] | None = None
        self.content.bind("<Configure>", self._content_resized)
        self.canvas.bind("<Configure>", self._canvas_resized)
        # The wheel is grabbed only while the pointer is over this area, so it never
        # steals scrolling from another window.
        self.canvas.bind("<Enter>", lambda _event: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda _event: self._unbind_wheel())
        self.bind("<Destroy>", lambda _event: self._unbind_wheel())

    def _content_resized(self, _event: tk.Event) -> None:
        region = self.canvas.bbox("all")
        if region is None or region == self._applied_region:
            return
        self._applied_region = region
        self.canvas.configure(scrollregion=region)

    def _canvas_resized(self, event: tk.Event) -> None:
        # Keep the inner frame as wide as the canvas so nothing is cut off sideways.
        if event.width == self._applied_width:
            return
        self._applied_width = event.width
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)  # X11
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.canvas.unbind_all(sequence)
            except tk.TclError:  # pragma: no cover - the window is already gone
                pass

    def _on_wheel(self, event: tk.Event) -> None:
        if not self._can_scroll():
            return
        self.canvas.yview_scroll(_wheel_steps(event), "units")

    def _can_scroll(self) -> bool:
        region = self.canvas.bbox("all")
        return bool(region) and region[3] > self.canvas.winfo_height()


def _wheel_steps(event: tk.Event) -> int:
    """One wheel notch, in the units each platform reports it in."""
    if getattr(event, "num", 0) == 4:
        return -1
    if getattr(event, "num", 0) == 5:
        return 1
    delta = int(getattr(event, "delta", 0))
    if abs(delta) >= 120:  # Windows reports multiples of 120
        return -delta // 120
    return -1 if delta > 0 else 1  # macOS reports small counts


class JobEditor(tk.Toplevel):
    """Edits one job in memory; nothing reaches disk until Save."""

    def __init__(
        self,
        parent: tk.Misc,
        job: JobConfig,
        *,
        on_saved: Callable[[JobConfig], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.job = job
        self.on_saved = on_saved
        self.title(f"Job — {job.name}")
        height = min(760, max(420, self.winfo_screenheight() - 140))
        self.geometry(f"720x{height}")
        self.minsize(560, 360)

        self.name_var = tk.StringVar(value=job.name)
        self.path_a_var = tk.StringVar(value=job.path_a)
        self.path_b_var = tk.StringVar(value=job.path_b)
        self.mode_var = tk.StringVar(value=job.mode)
        self.compare_var = tk.StringVar(value=job.compare)
        self.policy_var = tk.StringVar(value=job.deletion_policy)
        self.tolerance_var = tk.StringVar(value=str(job.mtime_tolerance_s))
        self.symlink_var = tk.BooleanVar(value=job.follow_symlinks)

        self._build()
        self._sync_tolerance_state()
        self._refresh_path_warnings()
        self.protocol("WM_DELETE_WINDOW", self._close)

    # --- layout ---------------------------------------------------------------

    def _build(self) -> None:
        # The buttons are packed first and anchored to the bottom, so they stay put
        # however tall the settings grow or however short the window is.
        buttons = ttk.Frame(self, padding=(14, 10))
        buttons.pack(side="bottom", fill="x")
        ttk.Button(buttons, text="Preview…", command=self.preview).pack(side="left")
        ttk.Button(buttons, text="Close", command=self._close).pack(side="right")
        ttk.Button(buttons, text="Save As…", command=self.save_as).pack(side="right", padx=(0, 8))
        ttk.Button(buttons, text="Save", command=self.save).pack(side="right", padx=(0, 8))
        ttk.Separator(self, orient="horizontal").pack(side="bottom", fill="x")

        self.scroll_area = ScrollableArea(self)
        self.scroll_area.pack(side="top", fill="both", expand=True)
        outer = ttk.Frame(self.scroll_area.content, padding=14)
        outer.pack(fill="both", expand=True)

        name_row = ttk.Frame(outer)
        name_row.pack(fill="x")
        ttk.Label(name_row, text="Name").pack(side="left")
        ttk.Entry(name_row, textvariable=self.name_var).pack(
            side="left", fill="x", expand=True, padx=(10, 0)
        )

        folders = ttk.LabelFrame(outer, text="Folders", padding=10)
        folders.pack(fill="x", pady=(12, 0))
        self.a_warning = self._folder_row(folders, "Folder A", self.path_a_var, 0)
        self.b_warning = self._folder_row(folders, "Folder B", self.path_b_var, 2)
        folders.columnconfigure(1, weight=1)
        self.path_a_var.trace_add("write", lambda *_: self._refresh_path_warnings())
        self.path_b_var.trace_add("write", lambda *_: self._refresh_path_warnings())

        mode = ttk.LabelFrame(outer, text="Mode", padding=10)
        mode.pack(fill="x", pady=(12, 0))
        self._radio_group(mode, MODE_CHOICES, self.mode_var)

        compare = ttk.LabelFrame(outer, text="How files are compared", padding=10)
        compare.pack(fill="x", pady=(12, 0))
        self._radio_group(
            compare, COMPARE_CHOICES, self.compare_var, on_change=self._sync_tolerance_state
        )
        tolerance_row = ttk.Frame(compare)
        tolerance_row.pack(fill="x", pady=(8, 0))
        self.tolerance_label = ttk.Label(tolerance_row, text="Timestamp tolerance (seconds)")
        self.tolerance_label.pack(side="left")
        self.tolerance_spin = ttk.Spinbox(
            tolerance_row, from_=0, to=3600, increment=1, width=8, textvariable=self.tolerance_var
        )
        self.tolerance_spin.pack(side="left", padx=(10, 0))
        ttk.Label(
            tolerance_row,
            text="survives FAT/SMB timestamp granularity",
            foreground=GREY,
        ).pack(side="left", padx=(10, 0))

        policy = ttk.LabelFrame(outer, text="When something has to be deleted", padding=10)
        policy.pack(fill="x", pady=(12, 0))
        self._radio_group(policy, POLICY_CHOICES, self.policy_var)

        options = ttk.LabelFrame(outer, text="Options", padding=10)
        options.pack(fill="x", pady=(12, 0))
        ttk.Checkbutton(
            options,
            text="Follow symbolic links (off: links are synced as links)",
            variable=self.symlink_var,
        ).pack(anchor="w")

        excludes = ttk.LabelFrame(outer, text="Exclusions", padding=10)
        excludes.pack(fill="both", expand=True, pady=(12, 0))
        ttk.Label(
            excludes,
            text="Glob patterns matched against the relative path or the name; "
            "a trailing / means a folder and its contents.",
            foreground=GREY,
        ).pack(anchor="w")
        list_row = ttk.Frame(excludes)
        list_row.pack(fill="both", expand=True, pady=(6, 0))
        self.exclude_list = tk.Listbox(list_row, height=5)
        self.exclude_list.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(list_row, orient="vertical", command=self.exclude_list.yview)
        self.exclude_list.configure(yscrollcommand=scroll.set)
        scroll.pack(side="left", fill="y")
        for pattern in self.job.exclude:
            self.exclude_list.insert("end", pattern)

        add_row = ttk.Frame(excludes)
        add_row.pack(fill="x", pady=(6, 0))
        self.new_pattern = tk.StringVar()
        entry = ttk.Entry(add_row, textvariable=self.new_pattern)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _event: self._add_pattern())
        ttk.Button(add_row, text="Add", command=self._add_pattern).pack(side="left", padx=(8, 0))
        ttk.Button(add_row, text="Remove", command=self._remove_pattern).pack(
            side="left", padx=(6, 0)
        )

    def _folder_row(self, parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> ttk.Label:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(0, 2))
        ttk.Entry(parent, textvariable=var).grid(row=row, column=1, sticky="ew", padx=(10, 8))
        ttk.Button(parent, text="Browse…", command=lambda: self._browse(var, label)).grid(
            row=row, column=2
        )
        warning = ttk.Label(parent, text="", foreground=RED)
        warning.grid(row=row + 1, column=1, sticky="w", padx=(10, 0), pady=(0, 6))
        return warning

    def _radio_group(
        self,
        parent: ttk.LabelFrame,
        choices: tuple[tuple[str, str, str], ...],
        variable: tk.StringVar,
        *,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        for value, label, helper in choices:
            ttk.Radiobutton(
                parent,
                text=label,
                value=value,
                variable=variable,
                command=on_change or (lambda: None),
            ).pack(anchor="w")
            ttk.Label(parent, text=f"    {helper}", foreground=GREY).pack(anchor="w", pady=(0, 4))

    # --- behaviour ------------------------------------------------------------

    def _browse(self, var: tk.StringVar, label: str) -> None:
        chosen = filedialog.askdirectory(parent=self, title=f"Choose {label}", mustexist=True)
        if chosen:
            var.set(chosen)

    def _sync_tolerance_state(self) -> None:
        """The tolerance only means anything under "size and timestamp"."""
        enabled = self.compare_var.get() == "size_mtime"
        state = "normal" if enabled else "disabled"
        self.tolerance_spin.configure(state=state)
        self.tolerance_label.configure(foreground="" if enabled else GREY)

    def _refresh_path_warnings(self) -> None:
        for var, label, widget in (
            (self.path_a_var, "Folder A", self.a_warning),
            (self.path_b_var, "Folder B", self.b_warning),
        ):
            problem = path_problem(var.get(), label)
            widget.configure(text=problem or "")

    def _add_pattern(self) -> None:
        pattern = self.new_pattern.get().strip()
        if pattern and pattern not in self.exclude_list.get(0, "end"):
            self.exclude_list.insert("end", pattern)
        self.new_pattern.set("")

    def _remove_pattern(self) -> None:
        for index in reversed(self.exclude_list.curselection()):
            self.exclude_list.delete(index)

    def apply_to_job(self) -> None:
        """Copy what is on screen into the JobConfig — still only in memory."""
        self.job.name = self.name_var.get().strip() or self.job.name
        self.job.path_a = self.path_a_var.get().strip()
        self.job.path_b = self.path_b_var.get().strip()
        self.job.mode = self.mode_var.get()  # type: ignore[assignment]
        self.job.compare = self.compare_var.get()  # type: ignore[assignment]
        self.job.deletion_policy = self.policy_var.get()  # type: ignore[assignment]
        try:
            self.job.mtime_tolerance_s = float(self.tolerance_var.get())
        except ValueError:
            self.job.mtime_tolerance_s = 2
            self.tolerance_var.set("2")
        self.job.follow_symlinks = bool(self.symlink_var.get())
        self.job.exclude = list(self.exclude_list.get(0, "end"))

    def save(self) -> bool:
        self.apply_to_job()
        if not self.job.name.strip():
            messagebox.showerror("Name required", "Give the job a name first.", parent=self)
            return False
        try:
            jobs.save(self.job)
        except JobError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return False
        self.title(f"Job — {self.job.name}")
        if self.on_saved:
            self.on_saved(self.job)
        return True

    def save_as(self) -> None:
        name = simpledialog.askstring("Save As", "Name for the copy:", parent=self)
        if not name:
            return
        self.apply_to_job()
        copy = jobs.duplicate(self.job, name)
        try:
            jobs.save(copy, path=jobs.unique_job_path(name))
        except JobError as exc:
            messagebox.showerror("Could not save", str(exc), parent=self)
            return
        self.job = copy
        self.name_var.set(copy.name)
        self.title(f"Job — {copy.name}")
        if self.on_saved:
            self.on_saved(copy)

    def preview(self) -> None:
        """Scan and plan. Reads only — this cannot write anything."""
        self.apply_to_job()
        problems = validate(self.job)
        if problems:
            messagebox.showerror(
                "Cannot preview yet",
                "\n".join(problems) + "\n\nFix these and try again.",
                parent=self,
            )
            return
        PreviewDialog(self, self.job, on_done=self._plan_ready)

    def _plan_ready(self, plan, error: str | None) -> None:
        if error:
            messagebox.showinfo("Preview", error, parent=self)
            return
        if plan is None:
            return
        PlanWindow(self, self.job, plan, on_repreview=self.preview)

    def _close(self) -> None:
        before = self.job.fingerprint(), self.job.name
        self.apply_to_job()
        if (self.job.fingerprint(), self.job.name) != before or self.job.file_path is None:
            answer = messagebox.askyesnocancel(
                "Unsaved changes", "Save this job before closing?", parent=self
            )
            if answer is None:
                return
            if answer and not self.save():
                return
        self.destroy()
