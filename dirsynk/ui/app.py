"""The start screen: the jobs you have saved, and what to do with one."""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .. import __version__
from ..core import jobs
from ..core.models import JobConfig
from ..core.validate import path_problem, validate
from .branding import APP_NAME, apply_icon, claim_app_name
from .job_editor import JobEditor
from .plan_view import PlanWindow
from .runner import PreviewDialog

COLUMNS = (
    ("name", "Job", 180),
    ("mode", "Mode", 90),
    ("compare", "Compared by", 150),
    ("path_a", "Folder A", 240),
    ("path_b", "Folder B", 240),
    ("last_run", "Last run", 170),
)

MODE_LABELS = {"mirror": "Mirror", "two_way": "Two-way"}
COMPARE_LABELS = {
    "size_mtime": "size and timestamp",
    "size_only": "size only",
    "content": "size and contents",
}


class App(tk.Tk):
    def __init__(self) -> None:
        # className is what X11 desktops label the window and its launcher with.
        super().__init__(className=APP_NAME)
        self.title(APP_NAME)
        self.geometry("1080x520")
        self.minsize(820, 400)
        self.rows: dict[str, JobConfig] = {}

        apply_icon(self)
        self._build_menu()
        self._build_body()
        self.refresh()

    # --- layout ---------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = tk.Menu(self)
        if sys.platform == "darwin":
            # A menu named "apple" *is* the application menu on macOS, and About belongs
            # at the top of it rather than under Help, which is where the rest of the
            # world expects it.
            app_menu = tk.Menu(menu, name="apple", tearoff=False)
            app_menu.add_command(label=f"About {APP_NAME}", command=self._about)
            menu.add_cascade(menu=app_menu)

        file_menu = tk.Menu(menu, tearoff=False)
        file_menu.add_command(label="New job…", command=self.new_job, accelerator="Ctrl+N")
        file_menu.add_command(label="Open selected", command=self.open_selected)
        file_menu.add_separator()
        file_menu.add_command(label="Reload list", command=self.refresh)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self.destroy)
        menu.add_cascade(label="File", menu=file_menu)

        if sys.platform != "darwin":
            help_menu = tk.Menu(menu, tearoff=False)
            help_menu.add_command(label=f"About {APP_NAME}", command=self._about)
            menu.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menu)
        self.bind("<Control-n>", lambda _event: self.new_job())

    def _build_body(self) -> None:
        header = ttk.Frame(self, padding=(14, 12, 14, 4))
        header.pack(fill="x")
        ttk.Label(header, text="Sync jobs", font=("TkDefaultFont", 15, "bold")).pack(side="left")
        ttk.Label(header, text=f"saved in {jobs.jobs_dir()}", foreground="#666666").pack(
            side="left", padx=(10, 0)
        )

        body = ttk.Frame(self, padding=(14, 4))
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            body, columns=[name for name, _, _ in COLUMNS], show="headings", selectmode="browse"
        )
        for name, heading, width in COLUMNS:
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, anchor="w")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="left", fill="y")
        self.tree.tag_configure("missing", foreground="#b00020")
        self.tree.bind("<Double-1>", lambda _event: self.open_selected())

        self.status = ttk.Label(self, text="", foreground="#b00020", padding=(14, 0))
        self.status.pack(fill="x")

        buttons = ttk.Frame(self, padding=(14, 10, 14, 14))
        buttons.pack(fill="x")
        for text, command in (
            ("New", self.new_job),
            ("Open", self.open_selected),
            ("Duplicate", self.duplicate_selected),
            ("Delete", self.delete_selected),
        ):
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Run…", command=self.run_selected).pack(side="right")

    # --- data -----------------------------------------------------------------

    def refresh(self) -> None:
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        loaded, problems = jobs.list_jobs()
        for job in loaded:
            missing = any(
                path_problem(path, label) for path, label in ((job.path_a, "A"), (job.path_b, "B"))
            )
            iid = self.tree.insert(
                "",
                "end",
                values=(
                    job.name,
                    MODE_LABELS.get(job.mode, job.mode),
                    COMPARE_LABELS.get(job.compare, job.compare),
                    job.path_a,
                    job.path_b,
                    job.last_run_utc or "never",
                ),
                tags=("missing",) if missing else (),
            )
            self.rows[iid] = job

        if problems:
            self.status.configure(
                text=f"{len(problems)} job file(s) could not be read: "
                + "; ".join(path.name for path, _ in problems)
            )
        elif not loaded:
            self.status.configure(text="No jobs yet — click New to make one.", foreground="#666666")
        else:
            self.status.configure(text="")

    def selected_job(self) -> JobConfig | None:
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Nothing selected", "Pick a job from the list first.", parent=self)
            return None
        return self.rows.get(selection[0])

    # --- actions --------------------------------------------------------------

    def new_job(self) -> None:
        name = simpledialog.askstring("New job", "Name for the new job:", parent=self)
        if not name:
            return
        JobEditor(self, jobs.new_job(name), on_saved=lambda _job: self.refresh())

    def open_selected(self) -> None:
        job = self.selected_job()
        if job is not None:
            JobEditor(self, job, on_saved=lambda _job: self.refresh())

    def duplicate_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        name = simpledialog.askstring(
            "Duplicate job", "Name for the copy:", initialvalue=f"{job.name} copy", parent=self
        )
        if not name:
            return
        copy = jobs.duplicate(job, name)
        jobs.save(copy, path=jobs.unique_job_path(name))
        self.refresh()

    def delete_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        if not messagebox.askokcancel(
            "Delete job",
            f"Delete the job “{job.name}”?\n\nThis removes the job file only. "
            "Neither folder is touched.",
            parent=self,
        ):
            return
        jobs.delete(job)
        self.refresh()

    def run_selected(self) -> None:
        job = self.selected_job()
        if job is None:
            return
        problems = validate(job)
        if problems:
            messagebox.showerror(
                "Cannot run this job",
                "\n".join(problems) + "\n\nOpen the job to fix its folders.",
                parent=self,
            )
            return
        PreviewDialog(self, job, on_done=lambda plan, error: self._plan_ready(job, plan, error))

    def _plan_ready(self, job: JobConfig, plan, error: str | None) -> None:
        if error:
            messagebox.showinfo("Preview", error, parent=self)
            return
        if plan is None:
            return
        PlanWindow(
            self,
            job,
            plan,
            on_repreview=lambda: self.run_selected(),
            on_finished=lambda _result: self.refresh(),
        )

    def _about(self) -> None:
        messagebox.showinfo(
            f"About {APP_NAME}",
            f"{APP_NAME} {__version__}\n\n"
            "Synchronises two folders, showing you every copy and delete before "
            "anything happens.\n\n"
            f"Jobs live in {jobs.jobs_dir()}.\n"
            "Deleted items are kept in a .deleted folder unless you choose otherwise; "
            "nothing ever empties it for you.",
            parent=self,
        )


def launch() -> None:
    """Open the app. ``python -m dirsynk`` with no arguments lands here."""
    claim_app_name()  # has to happen before Tk starts: see branding.claim_app_name
    App().mainloop()
