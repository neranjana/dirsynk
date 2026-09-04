"""The command line front end: ``python -m dirsynk --job NAME``.

Same engine as the GUI, same rule — the plan is printed in full and nothing is written
until you say yes (or pass ``--yes``).
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Sequence

from .core import jobs
from .core.executor import RunEvent, execute
from .core.jobs import JobError
from .core.models import COMPARE_MODES, JobConfig, Plan, human_bytes
from .core.planner import build_snapshot, preview
from .core.scanner import scan
from .core.validate import validate

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PARTIAL = 3
EXIT_CANCELLED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dirsynk",
        description="Synchronise two folders from a saved job, showing the plan first.",
    )
    parser.add_argument("--job", metavar="NAME", help="name (or slug) of a job in ~/.dirsynk")
    parser.add_argument("--list", action="store_true", help="list saved jobs and exit")
    parser.add_argument("--yes", action="store_true", help="run without asking to confirm")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan and exit, writing nothing"
    )
    parser.add_argument(
        "--compare",
        choices=COMPARE_MODES,
        help="override the job's comparison criterion for this run only",
    )
    return parser


def render_plan(plan: Plan, job: JobConfig) -> str:
    """The plan as text, in the same terms the GUI shows it."""
    lines = [
        f"Job: {job.name}",
        f"Mode: {'mirror (A → B)' if job.mode == 'mirror' else 'two-way'}"
        f"  ·  {plan.compared_by()}"
        f"  ·  Deletions: {job.deletion_policy}",
        f"A: {job.root_a}",
        f"B: {job.root_b}",
        f"Scanned: {plan.scanned_a} items in A, {plan.scanned_b} in B",
    ]
    if plan.first_run:
        lines.append("First two-way run: no snapshot yet, so nothing will be deleted.")
    lines.append("")

    if not plan.actions:
        lines.append("Nothing to do.")
    else:
        for action in plan.sorted_actions():
            size = human_bytes(action.size) if action.size else ""
            lines.append(
                f"  {action.kind:<12} {action.side:<7} {str(action.rel):<40} "
                f"{size:>9}  {action.reason}"
            )
    lines.append("")
    lines.append(plan.summary_line())
    if plan.errors:
        lines.append("")
        lines.append(f"{len(plan.errors)} path(s) could not be scanned:")
        lines.extend(f"  {message}" for message in plan.errors)
    return "\n".join(lines)


def destructive_note(plan: Plan, job: JobConfig) -> str | None:
    """The sentence the confirmation has to say out loud, or None if nothing is deleted."""
    count = plan.n_deletes
    if not count:
        return None
    size = human_bytes(plan.bytes_to_delete)
    if job.deletion_policy == "quarantine":
        return f"{count} item(s) ({size}) will be moved to .deleted."
    return f"WARNING: {count} item(s) ({size}) will be permanently deleted. This cannot be undone."


def _list_jobs(stream) -> int:
    loaded, problems = jobs.list_jobs()
    if not loaded and not problems:
        print(f"No jobs saved in {jobs.jobs_dir()}.", file=stream)
        return EXIT_OK
    for job in loaded:
        last = job.last_run_utc or "never run"
        print(f"{job.name}  [{job.mode}, {job.compare}]  {job.path_a} → {job.path_b}  ({last})")
    for path, message in problems:
        print(f"! {path.name}: {message}", file=stream)
    return EXIT_OK


def _update_job_after_run(job: JobConfig, result, plan: Plan) -> None:
    """Record the run, and in two-way mode remember the state both sides now agree on."""
    job.last_run_utc = jobs.utc_now()
    job.last_result = result.to_json()
    if job.mode == "two_way" and not result.errors and not result.cancelled:
        scan_a = scan(job.root_a, exclude=job.exclude, follow_symlinks=job.follow_symlinks)
        scan_b = scan(job.root_b, exclude=job.exclude, follow_symlinks=job.follow_symlinks)
        if not scan_a.errors and not scan_b.errors:
            job.snapshot = build_snapshot(scan_a.entries, scan_b.entries)
    if job.file_path is not None:
        jobs.save(job)


def main(argv: Sequence[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        from .ui.app import launch  # imported here so the CLI never needs a display

        launch()
        return EXIT_OK

    parser = build_parser()
    options = parser.parse_args(args)

    if options.list:
        return _list_jobs(sys.stderr)
    if not options.job:
        parser.error("either --job NAME or --list is required")

    try:
        job = jobs.find_by_name(options.job)
    except JobError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    if job is None:
        print(f"No job named {options.job!r} in {jobs.jobs_dir()}.", file=sys.stderr)
        return EXIT_ERROR

    problems = validate(job)
    if problems:
        print(f"Cannot run {job.name!r}:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return EXIT_ERROR

    plan, _, _ = preview(job, compare=options.compare)
    print(render_plan(plan, job))

    if options.dry_run:
        print("\nDry run: nothing was written.")
        return EXIT_OK
    if not plan.has_work:
        return EXIT_OK

    note = destructive_note(plan, job)
    if not options.yes:
        if note:
            print(f"\n{note}")
        answer = input("\nProceed? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Nothing was written.")
            return EXIT_CANCELLED
    elif note:
        print(f"\n{note}")

    cancel = threading.Event()

    def report(event: RunEvent) -> None:
        if event.kind == "log":
            print(f"  {event.message}")

    result = execute(plan, job, cancel=cancel, on_event=report)
    print()
    print(result.summary_line())
    for error in result.errors:
        print(f"  {error.rel}: {error.message}", file=sys.stderr)

    _update_job_after_run(job, result, plan)

    if result.cancelled:
        return EXIT_CANCELLED
    return EXIT_PARTIAL if result.failed else EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
