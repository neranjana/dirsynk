"""One smoke test that the windows build. Skipped wherever there is no display."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from helpers import build

from dirsynk.core import jobs

tk = pytest.importorskip("tkinter")

NO_DISPLAY = sys.platform.startswith("linux") and not os.environ.get("DISPLAY")
pytestmark = pytest.mark.skipif(NO_DISPLAY, reason="no display available")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(jobs.HOME_ENV_VAR, str(tmp_path / "home"))
    from dirsynk.ui.app import App

    try:
        app = App()
    except tk.TclError as exc:  # pragma: no cover - depends on the machine
        pytest.skip(f"Tk could not open a display: {exc}")
    app.withdraw()
    yield app
    app.destroy()


def test_the_main_window_builds_lists_jobs_and_closes(root, tmp_path: Path) -> None:
    job = jobs.new_job("Smoke", str(tmp_path / "a"), str(tmp_path / "b"))
    jobs.save(job)
    root.refresh()
    root.update()
    assert [root.tree.item(iid, "values")[0] for iid in root.tree.get_children()] == ["Smoke"]


def test_the_plan_window_shows_rows_and_a_summary(root, tmp_path: Path) -> None:
    from dirsynk.core.planner import preview
    from dirsynk.ui.plan_view import PlanWindow

    a = build(tmp_path / "a", {"new.txt": "hello", "empty": None})
    b = build(tmp_path / "b", {"stale.txt": "x"})
    job = jobs.new_job("Smoke", str(a), str(b))
    plan, _, _ = preview(job)

    window = PlanWindow(root, job, plan, on_repreview=lambda: None)
    window.withdraw()
    window.update()

    assert "1 to copy" in window.summary_label.cget("text")
    assert "1 to delete" in window.summary_label.cget("text")
    assert window.plan.n_copies == 1

    # Unticking a row takes it out of the run and out of the summary.
    iid = next(iter(window.row_action))
    window._toggle(iid)
    assert window.plan.actions[window.row_action[iid]].included is False
    window.destroy()


def test_the_job_editor_greys_out_the_tolerance_for_other_criteria(root, tmp_path: Path) -> None:
    from dirsynk.ui.job_editor import JobEditor

    job = jobs.new_job("Smoke", str(tmp_path / "a"), str(tmp_path / "b"))
    editor = JobEditor(root, job)
    editor.withdraw()
    # update_idletasks, not update: on macOS, update() on a withdrawn window holding a
    # canvas-embedded frame never returns.
    editor.update_idletasks()

    assert str(editor.tolerance_spin.cget("state")) == "normal"
    editor.compare_var.set("size_only")
    editor._sync_tolerance_state()
    assert str(editor.tolerance_spin.cget("state")) == "disabled"

    # A folder that does not exist is called out in red before anything is attempted.
    editor._refresh_path_warnings()
    assert "path not found" in editor.a_warning.cget("text")
    editor.destroy()


def test_the_job_editor_scrolls_when_the_window_is_short(root, tmp_path: Path) -> None:
    from dirsynk.ui.job_editor import JobEditor

    job = jobs.new_job("Smoke", str(tmp_path / "a"), str(tmp_path / "b"))
    editor = JobEditor(root, job)
    editor.geometry("720x420")  # shorter than the settings need
    # Left mapped on purpose: a withdrawn window is never laid out, so its real
    # heights — the whole point of this test — would all read as 1 pixel.
    editor.update_idletasks()
    editor.update()

    canvas = editor.scroll_area.canvas
    content_height = canvas.bbox("all")[3]
    assert content_height > canvas.winfo_height(), "expected the settings to overflow"

    # The scrollbar reaches the bottom of the content...
    canvas.yview_moveto(1.0)
    editor.update()
    assert canvas.yview()[1] == pytest.approx(1.0)

    # ...and the buttons are pinned below it, on screen, whatever the content does.
    footer = editor.winfo_children()[0]
    assert footer.winfo_viewable()
    assert footer.winfo_y() + footer.winfo_height() <= editor.winfo_height()
    labels = {
        child.cget("text") for child in footer.winfo_children() if child.winfo_class() == "TButton"
    }
    assert {"Save", "Save As…", "Preview…", "Close"} <= labels
    editor.destroy()


def test_the_window_is_named_and_iconed_dirsynk(root) -> None:
    from dirsynk.ui import branding

    assert root.title() == "DirsyNK"
    # One PhotoImage per shipped size, held by the window so Tk's icon survives.
    assert len(root.icon_images) == len(branding.icon_paths()) == len(branding.ICON_SIZES)
    assert root.icon_images[0].width() == max(branding.ICON_SIZES)
