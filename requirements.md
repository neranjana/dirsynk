# Meta-prompt: build `dirsynk`


## Task

Build **`dirsynk`**, a cross-platform Python desktop application that synchronises two
user-selected directories. It must show the user a **plan** of every copy and delete it
intends to perform, and only act after explicit confirmation. Sync configurations ("jobs")
are saved as JSON under `~/.dirsynk/` and can be reopened and re-run later.

Work in plan mode first: read this whole brief, write an implementation plan to
`PLAN.md`, and ask me about anything in the "Open questions" section below before you
start writing code. Then implement it module by module, running tests as you go.

## Hard constraints

- Python 3.11+. **Standard library only at runtime** — GUI is `tkinter` / `tkinter.ttk`.
  Dev/test dependencies (`pytest`, `ruff`) are fine.
- Must run on macOS, Windows and Linux. All path handling via `pathlib`; never assume `/`.
- Full type hints. `ruff check` and `ruff format` clean.
- **The core sync engine must have zero UI imports.** `dirsynk.core` is pure, synchronous,
  fully unit-testable, and drives both the GUI and a small CLI.
- No database, no async, no plugin system, no config framework. Do not over-engineer.

## Architecture

```
dirsynk/
  __main__.py          # python -m dirsynk  -> launches GUI
  cli.py               # python -m dirsynk --job NAME [--yes] [--dry-run]
                       #   [--compare {size_mtime,size_only,content}]
  core/
    models.py          # dataclasses: JobConfig, Entry, Action, Plan, RunResult
    scanner.py         # walk a tree -> dict[relpath, Entry]
    planner.py         # (source tree, dest tree, snapshot, config) -> Plan
    executor.py        # apply a Plan, emitting progress events
    deleter.py         # the two deletion policies
    jobs.py            # load/save/list job JSON in ~/.dirsynk
  ui/
    app.py             # main window, job list, menu
    job_editor.py      # directory pickers + options
    plan_view.py       # ttk.Treeview of the plan, per-row include/exclude
    runner.py          # worker thread + progress dialog + result summary
tests/
```

### Threading rule

The GUI must never block. All scanning, planning and execution happen on a
`threading.Thread`; the worker pushes progress/log/completion events onto a
`queue.Queue`, and the Tk main loop drains it with `root.after(100, ...)`. **No Tk calls
from the worker thread.** Long runs must be cancellable via a `threading.Event` that the
executor checks between items.

## Data model

```python
@dataclass(frozen=True)
class Entry:
    rel: PurePosixPath      # path relative to the root, '/'-normalised for portability
    kind: Literal["file", "dir"]
    size: int               # 0 for dirs
    mtime_ns: int
```

```python
CompareMode = Literal["size_mtime", "size_only", "content"]   # JobConfig.compare
```

```python
@dataclass(frozen=True)
class Action:
    kind: Literal["mkdir", "copy_new", "copy_update", "delete_file",
                  "delete_dir", "conflict", "skip"]
    rel: PurePosixPath
    direction: Literal["a_to_b", "b_to_a"]   # which side is being written to
    reason: str             # human-readable, and must name what triggered it:
                            # "new in A", "A newer by 3.2s", "sizes differ (4 KB vs 8 KB)",
                            # "same size, contents differ", "removed from A"
    size: int
    src: Path | None
    dst: Path | None
    included: bool = True   # user can untick rows in the plan view
```

`Plan` holds `list[Action]` plus totals (counts by kind, total bytes to copy, total bytes
to delete) and any `errors` collected during scanning.

## Sync semantics

The job has a **mode**, chosen by the user:

**1. Mirror (A → B).** A is the source of truth.
- In A, not in B → copy (`copy_new`); create dirs, including **empty ones** (`mkdir`).
- In both, differ **under the job's comparison criterion** → `copy_update`.
- In B, not in A → delete, per the deletion policy.

**2. Two-way.** Uses a snapshot of the last successful run stored in the job file
(`rel -> {kind, size, mtime_ns}`, recorded for the state after that run).
For each path in the union of both trees:
- In both, identical → nothing.
- In both, differ **under the job's comparison criterion** → newer `mtime_ns` wins, copy
  over the other side. The criterion decides *whether* two files differ; the winning side
  is **always the newer `mtime_ns`, under every criterion**, because a difference still has
  to be resolved to a direction. Under `size_only` that reads
  "sizes differ, A newer by 52m". If **both** sides changed since the snapshot, emit a
  `conflict` action: default resolution is newer-wins, but the row is highlighted and the
  user can untick it to skip.
- In one side only:
  - present in the snapshot → it was deleted on the other side → delete it here, per policy.
  - absent from the snapshot → it is new → copy it to the other side.
- In neither, but in the snapshot → drop from the snapshot.
- **First run (no snapshot): union copy, delete nothing.** State this in the UI.

"Changed since the snapshot" is **always** evaluated as size differing, or `mtime_ns`
differing beyond the tolerance — regardless of the chosen criterion. The snapshot exists to
detect edits since the last run, not to decide equality between the two sides; keeping it
criterion-independent means the snapshot schema never has to carry a hash.

### Comparison criterion

How "same file" is decided is a **per-job choice made by the user** in the job editor, not
a fixed rule. `compare` is one of three values:

- **`size_mtime`** (the default) — two files differ if `size` differs, **or** if `mtime_ns`
  differs by more than `mtime_tolerance_s` (default 2 s, to survive FAT/SMB timestamp
  granularity). The sensible general-purpose choice.
- **`size_only`** — two files differ **if and only if** `size` differs. Timestamps are
  ignored entirely when deciding equality. The cheapest option, and the right one for
  archival trees or destinations that do not preserve mtime reliably (some SMB shares, some
  cloud mounts, anything unzipped fresh). State the trade-off plainly in the UI: **an edit
  that does not change the file's size is invisible to this criterion.**
- **`content`** — two files differ if `size` differs, **or** if the sizes match and their
  SHA-256 digests differ. Exact and slowest. Hashing runs **only** when sizes are equal —
  a size mismatch is already conclusive, so never hash in that case. The planner hashes
  lazily and caches per path, so no file is read twice while building one plan.

Rules that apply across all three:

- `mtime_tolerance_s` is used **only** by `size_mtime`; the other two ignore it.
- Directories are never subject to the criterion — a directory matches a directory. The
  criterion applies to file-vs-file comparison only.
- A kind mismatch (file on one side, directory on the other) is never "same" under any
  criterion; treat it as a `conflict`.

### Copy safety

Copies use `shutil.copy2` so mtime is preserved; write to a `.dirsynk.tmp` sibling then
`os.replace` onto the target so an interrupted copy never leaves a truncated file in
place.

### Empty directories

First-class citizens throughout. The scanner records directories as entries, not just as
implied parents. An empty directory present on one side and absent on the other is a real
`mkdir` or `delete_dir` action and must appear in the plan. Directory deletes execute
deepest-first, after all file deletes.

## Deletion policy

Per job, a radio choice:

- **`permanent`** — `os.remove` / `os.rmdir`.
- **`quarantine`** — move the item into a `.deleted` directory **at the root of the
  directory it is being removed from** (so each of the two roots gets its own `.deleted`),
  preserving the relative path under a per-run timestamp folder:
  `<root>/.deleted/<YYYYMMDD-HHMMSS>/<rel path>`. Cross-device moves fall back to
  copy-then-remove. Name collisions get a ` (2)` suffix rather than overwriting.

`.deleted` and `.dirsynk` are **always excluded from scanning** on both sides, in both
modes, and can never be a sync source or target. Quarantine is the default in the UI.

## Job files

Stored at `~/.dirsynk/<slug>.json`, created with `parents=True, exist_ok=True`. Written
atomically (temp file + `os.replace`), UTF-8, `indent=2`.

```json
{
  "schema_version": 1,
  "name": "Photos to NAS",
  "path_a": "/Users/me/Photos",
  "path_b": "/Volumes/nas/Photos",
  "mode": "mirror",
  "deletion_policy": "quarantine",
  "compare": "size_mtime",
  "mtime_tolerance_s": 2,
  "follow_symlinks": false,
  "exclude": ["*.tmp", ".DS_Store", "Thumbs.db", "__pycache__/"],
  "created_utc": "2026-09-04T00:00:00Z",
  "last_run_utc": null,
  "last_result": null,
  "snapshot": {}
}
```

- Reject unknown `schema_version` with a clear message rather than guessing.
- `compare` must be one of `"size_mtime"`, `"size_only"`, `"content"`. An unrecognised
  value is **rejected with a clear message**, the same as an unknown `schema_version` —
  never silently fall back. A job file missing the key altogether loads as `"size_mtime"`.
- `mtime_tolerance_s` is kept in the file even while unused, so switching the criterion
  away from `size_mtime` and back does not lose the setting.
- A job file whose directories no longer exist must still open — the UI shows the paths in
  red with a "path not found" note and refuses to plan until they are fixed.
- `snapshot` is only populated in two-way mode, and only after a run that completed with
  no errors on the affected paths.
- Exclusions are `fnmatch` glob patterns matched against the relative path; a trailing `/`
  means directory-and-contents.

## User flow

1. **Start screen** — list of saved jobs from `~/.dirsynk` (name, both paths, mode, last
   run). Buttons: New, Open, Duplicate, Delete, Run. Double-click opens a job.
2. **Job editor** — two directory pickers (`filedialog.askdirectory`), mode radio,
   deletion-policy radio, exclusion list, symlink checkbox. Save / Save As.
   The **comparison criterion is a three-way radio group** with plain-language labels and
   one line of helper text each:
   - "Size and timestamp (recommended)" — *files differ if their size or modified time
     differs*
   - "Size only" — *ignores timestamps; a same-size edit will not be detected*
   - "Size and contents (SHA-256)" — *exact, slowest; only hashes when sizes match*

   The `mtime_tolerance_s` spinbox is enabled only for "Size and timestamp" and greyed out
   for the other two.
3. **Preview** — runs scan + plan on a worker thread with an indeterminate progress bar and
   a live "scanned N items" count. Result is a `ttk.Treeview`:
   - columns: Action, Side, Path, Type, Size, Reason
   - grouped by action kind, sortable by column, with a filter box
   - a checkbox column so individual rows can be excluded from this run
   - conflicts and deletes visually distinguished (colour tag)
   - a summary bar: "142 to copy (2.3 GB) · 8 to delete (110 MB) · 3 conflicts"
   - the **active criterion shown alongside the plan** — "Compared by: size only" — so a
     surprising plan explains itself
   - **"Nothing to do" is a valid, clearly-stated outcome.**

   Changing the criterion invalidates the current plan: re-preview is required, and a
   stale plan must never be executable against a changed setting.
4. **Confirm** — an explicit dialog naming the destructive part:
   "8 items will be moved to `.deleted`" or, for permanent deletion, a stronger warning.
   Nothing is written before this click. Ever.
5. **Execute** — determinate progress bar (by bytes for copies, by count for deletes),
   current-item label, scrolling log, Cancel button. Ordering: `mkdir` (parents first) →
   copies → file deletes → dir deletes (deepest first).
6. **Summary** — copied / deleted / skipped / failed counts, elapsed time, and an
   expandable list of per-item errors. Offer "Save log…". Then update `last_run_utc`,
   `last_result` and (two-way) `snapshot`, and write the job file.

## Safety rules

Validate before planning and refuse with a specific message:

- A and B are the same directory (resolve symlinks and case before comparing).
- One path is nested inside the other.
- Either path does not exist, is not a directory, or is not readable.
- Destination is not writable.
- A path lies inside a `.deleted` or `.dirsynk` directory.

During execution: **a per-item failure never aborts the run.** Catch `OSError`, record it
against the item, continue, and report everything at the end. Symlinks are not followed by
default and are recorded as their own entry kind; if `follow_symlinks` is on, detect cycles.
Skip anything the OS reports as a special file (fifo, socket, device).

## Tests (`pytest`, using `tmp_path`)

Core must be covered without a display:

- scanner: nested trees, empty dirs, exclusions, unreadable subdir, symlink handling
- planner, mirror: new file, changed file, identical file, extra file in dest,
  empty dir added, empty dir removed
- planner, comparison criteria — the matrix this feature exists for:
  - same size, different mtime → `copy_update` under `size_mtime`; **no action** under
    `size_only`; under `content`, no action when the bytes match and `copy_update` when
    they do not
  - different size, same mtime → `copy_update` under all three
  - mtime differing by less than `mtime_tolerance_s` → no action under `size_mtime`
  - `content`: spy on the hash helper and assert files of **differing size are never
    hashed**, and that a file is hashed at most once per plan
- planner, two-way + `size_only`: sizes differ on both sides → the newer `mtime_ns` picks
  the direction
- planner, two-way: first run with no snapshot deletes nothing; delete on one side
  propagates; edit on one side propagates; simultaneous edits produce a conflict
- deleter: permanent removal; quarantine puts the file at the expected `.deleted` path with
  its relative structure intact; collision suffixing; nested empty dirs
- executor: interrupted copy leaves no partial target; a permission error on one file does
  not stop the rest; deletion ordering removes directories only when empty
- jobs: round-trip save/load; atomic write; unknown schema version rejected;
  unknown `compare` value rejected; a file with no `compare` key loads as `size_mtime`;
  missing directory still opens
- one GUI smoke test that builds the main window and exits, skipped when no display
  (`pytest.mark.skipif` on `DISPLAY`/platform)

Add `--dry-run` and `--compare` to the CLI and use them in an end-to-end test that asserts
the plan text for a fixture tree, including that `--compare size_only` changes that output.
`--compare` overrides the saved job **for that run only** and is never written back to the
job file.

## Deliverables

- The package as laid out above
- `pyproject.toml` with a `dirsynk` console-script entry point, `pytest` + `ruff` dev extras
- `README.md`: what it does, install, run, the two modes explained in plain language, the
  three comparison criteria and when to pick each (including that "size only" cannot see a
  same-size edit), where jobs live, what `.deleted` is and that nothing purges it
  automatically
- `PLAN.md` from the planning step, kept updated

## Working agreement

- Write `PLAN.md` first and confirm it with me before implementing.
- Build in this order: `models` → `scanner` → `planner` → `deleter` → `executor` → `jobs`
  → `cli` → GUI. Tests alongside each module, not at the end.
- Run `pytest` and `ruff check` after each module and fix before moving on.
- Commit per module, with a message describing the behaviour added.
- If a requirement here is ambiguous or turns out to be wrong, stop and ask rather than
  inventing a behaviour and burying it.
- Show me the plan-view screenshot or a text render of the plan output once the GUI runs.

## Definition of done

- [ ] `python -m dirsynk` opens the app on a clean machine with no pip installs
- [ ] A job can be created, saved, closed, reopened and re-run from `~/.dirsynk`
- [ ] Empty directories are created and removed correctly in both modes
- [ ] A file identical in size but with a different mtime is copied under "size and
      timestamp" and produces no action under "size only"
- [ ] `.deleted` quarantine reproduces the original relative path and never overwrites
- [ ] No filesystem write of any kind occurs before the confirmation click
- [ ] A 50k-file tree previews without freezing the UI, and Cancel works mid-run
- [ ] A read-only file in the destination produces a reported error, not a crash
- [ ] `pytest` green, `ruff check` clean

## Open questions — ask me before coding

1. Should the two-way snapshot live inside the job JSON (simple, one file) or in a separate
   `~/.dirsynk/state/<slug>.json` (keeps the job file small and human-editable)?
2. Should `.deleted` have any retention behaviour (prune folders older than N days), or is
   it purely manual cleanup?
3. Is a scheduling / "run on a timer" feature wanted, or is manual-run only correct for v1?
