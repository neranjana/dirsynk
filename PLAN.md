# PLAN.md — `dirsynk`

Implementation plan for the brief in `requirements.md`. **Status: built.** Every module
below is implemented, tested and committed; the notes marked *(as built)* record where the
finished code differs from, or adds to, the plan as first written.

## 0. Decisions taken (answers to the Open questions)

1. **Snapshot lives inside the job JSON** (`"snapshot": {}`), exactly as the documented
   schema shows. One file per job, self-contained and portable; no `state/` directory to
   keep in sync across rename/duplicate/delete.
2. **`.deleted` is never pruned automatically.** Purely manual cleanup. The README says so
   plainly. No retention setting in v1.
3. **Manual run only.** No scheduler, no timer, no tray lifecycle. Anyone who wants
   automation uses the CLI under cron / Task Scheduler. This also keeps the "nothing is
   written before the confirmation click" rule honest — an unattended GUI run would need an
   auto-confirm escape hatch.

## 1. Stated assumptions (points the brief leaves open; flagged rather than buried)

- **`Entry.kind` gains a third value, `"symlink"`.** The data model says
  `Literal["file", "dir"]`, but the safety rules say symlinks "are recorded as their own
  entry kind". Those cannot both hold, so the literal is widened to
  `Literal["file", "dir", "symlink"]`. It only ever appears when `follow_symlinks` is off;
  with the flag on, symlinks resolve to `file`/`dir` as usual (with cycle detection).
  Two symlinks are "same" when their targets match; a symlink opposite a file or dir is a
  kind mismatch, so a `conflict`. Copying one recreates the link
  (`shutil.copy2(..., follow_symlinks=False)`), it never copies through it.
- **Exclusion patterns match the relative path *or* the basename.** `fnmatch` against the
  relative path alone would make the brief's own default `".DS_Store"` miss
  `sub/.DS_Store`, which is plainly not the intent. A trailing `/` (`__pycache__/`)
  restricts the pattern to directories and prunes the whole subtree.
- **Special files** (fifo, socket, device) are skipped by the scanner and reported in a
  separate `skipped` list, not in `errors` — they are an expected condition, and `errors`
  is what suppresses the snapshot update after a two-way run.
- **Scan errors are per-path and non-fatal**, mirroring the execution rule: an unreadable
  subdirectory is recorded and the walk continues.
- The **plan is invalidated** by any edit to the job settings, not only the criterion: the
  plan view holds the config fingerprint it was built from, and Run refuses if it changed.

## 2. Layout

```
dirsynk/
  __init__.py          # __version__
  __main__.py          # python -m dirsynk -> GUI, or CLI when args are present
  cli.py               # --job NAME [--yes] [--dry-run] [--compare ...] [--list]
  core/
    __init__.py
    models.py          # Entry, Action, Plan, RunResult, JobConfig, enums, format helpers
    scanner.py         # scan(root, ...) -> ScanResult
    planner.py         # build_plan(a, b, config, snapshot, ...) -> Plan
    deleter.py         # remove(path, root, policy, stamp)
    executor.py        # execute(plan, config, cancel, on_event) -> RunResult
    jobs.py            # load/save/list/delete/duplicate under ~/.dirsynk
  ui/
    __init__.py
    app.py             # main window: job list + menu
    job_editor.py      # pickers, mode/policy/criterion radios, exclusions, tolerance
    plan_view.py       # Treeview of the plan, per-row include/exclude, filter, summary
    runner.py          # worker thread, queue pump, progress dialog, confirm, summary
tests/
pyproject.toml
README.md
PLAN.md
```

`dirsynk.core` imports nothing from `dirsynk.ui` and nothing from `tkinter`; a test asserts
this by scanning the core sources for forbidden imports.

## 3. Module-by-module build order

Each step: write module → write its tests → `pytest` + `ruff check` + `ruff format` →
commit with a message describing the behaviour added.

### 3.1 `models.py`
Frozen dataclasses `Entry`, `Action`; `Plan` (actions, errors, and derived totals: counts
by kind, bytes to copy, bytes to delete); `RunResult` (copied/deleted/skipped/failed,
bytes, elapsed, per-item errors); `JobConfig` with every JSON field plus `snapshot`.
Literals for `CompareMode`, `SyncMode`, `DeletionPolicy`, `EntryKind`, `ActionKind`.
Helpers: `human_bytes`, `human_duration` (used in reason strings, so they live in core).

### 3.2 `scanner.py`
`os.scandir` walk, iterative, `lstat` per entry. Records **directories as entries**, empty
ones included. Always prunes any path component named `.deleted` or `.dirsynk`. Applies
exclusions. Returns `ScanResult(entries: dict[PurePosixPath, Entry], errors, skipped)` and
calls an optional `progress(n_scanned)` callback plus an optional `cancel` Event check.
Relative paths are `PurePosixPath` built from parts, so Windows separators never leak.

*Tests:* nested trees, empty dirs, exclusion globs (file, basename, `dir/`), unreadable
subdir recorded not raised, symlink recorded as its own kind, `follow_symlinks=True` cycle
detection, `.deleted`/`.dirsynk` always skipped.

### 3.3 `planner.py`
`build_plan(a_entries, b_entries, config, snapshot, root_a, root_b, hasher=sha256_file)`.

- `_files_differ(...) -> tuple[bool, str]` implements the three criteria and returns the
  human reason. `content` hashes **only when sizes are equal**, through a per-plan
  `_HashCache` keyed by absolute path so no file is read twice; `hasher` is injectable so a
  test can spy on it.
- Mirror: union walk; `copy_new` / `mkdir` / `copy_update` / `delete_file` / `delete_dir`.
- Two-way: snapshot-driven, per the brief. "Changed since snapshot" is always
  size-or-mtime-beyond-tolerance, criterion-independent. Both sides changed → `conflict`
  (default resolution newer-wins, row highlighted, untickable). No snapshot → union copy,
  delete nothing, and the plan carries a `first_run` flag the UI states out loud.
- Kind mismatch → `conflict` under every criterion.
- Reasons follow the brief's wording: `"new in A"`, `"A newer by 3.2s"`,
  `"sizes differ (4 KB vs 8 KB)"`, `"same size, contents differ"`, `"removed from A"`.

*Tests:* the full matrix the brief lists, including the hash-spy assertions
(differing sizes are never hashed; each file hashed at most once per plan) and
two-way + `size_only` direction-by-mtime.

### 3.4 `deleter.py`
`remove(path, root, policy, stamp)`. `permanent` → `os.remove` / `os.rmdir`.
`quarantine` → move under `<root>/.deleted/<YYYYMMDD-HHMMSS>/<rel>`, parents created,
`os.replace` where possible and copy-then-remove on `EXDEV`, collisions suffixed
` (2)`, ` (3)`, … never overwritten.

*Tests:* permanent removal, quarantine path/structure, collision suffixing, nested empty
dirs, cross-device fallback (simulated by forcing `EXDEV`).

### 3.5 `executor.py`
`execute(plan, config, cancel, on_event) -> RunResult`. Order: `mkdir` shallowest-first →
copies → `delete_file` → `delete_dir` deepest-first. Only `included` actions run.
Copies go to a `<name>.dirsynk.tmp` sibling then `os.replace`; the temp file is removed on
failure, so an interrupted copy never leaves a truncated target. Every item is wrapped in
`try/except OSError`: record against the item, continue, report at the end. `cancel` is
checked between items (and between chunks of a large copy). Progress events
(`ItemStarted`, `BytesAdvanced`, `ItemDone`, `LogLine`, `Finished`) go to `on_event`, which
the UI turns into `queue.Queue` puts — the executor itself knows nothing about Tk.

*Tests:* interrupted copy leaves no partial target and no stray tmp, a permission error on
one file does not stop the rest, directories are removed only after their contents,
cancellation stops between items.

### 3.6 `jobs.py`
`~/.dirsynk` created `parents=True, exist_ok=True`. `save(job)` writes temp + `os.replace`,
UTF-8, `indent=2`. `load(path)` rejects unknown `schema_version` and unrecognised
`compare` with clear messages; a missing `compare` key loads as `size_mtime`;
`mtime_tolerance_s` is preserved even when unused. `list_jobs()` returns loadable jobs and
reports unloadable ones instead of crashing. A job whose directories are gone still loads.
Slug derived from the name, collision-safe.

*Tests:* round-trip, atomic write (no partial file on failure), unknown schema version,
unknown `compare`, missing `compare`, missing directories still open.

### 3.7 `validate.py` (inside core; small)
Pre-plan safety checks returning a list of specific messages: same directory after
`resolve()` + case-fold, one nested inside the other, missing / not-a-directory /
unreadable, destination not writable, path inside `.deleted` or `.dirsynk`.

*Tests:* one per rule.

### 3.8 `cli.py`
`python -m dirsynk --job NAME [--yes] [--dry-run] [--compare {...}] [--list]`.
Renders a stable text plan (the same summary line as the GUI, plus one line per action) and
the active criterion. `--dry-run` prints and exits without writing. `--yes` skips the
prompt. `--compare` overrides the saved job **for that run only** and is never written back.

*Tests:* end-to-end on a fixture tree asserting the plan text, and that
`--compare size_only` changes that output; a test that `--compare` does not touch the file.

### 3.9 GUI (`ui/`)
- `app.py` — job list (name, both paths, mode, last run); New / Open / Duplicate / Delete /
  Run; double-click opens.
- `job_editor.py` — two `askdirectory` pickers, mode radio, deletion-policy radio
  (quarantine default), **three-way criterion radio with the brief's labels and helper
  text**, tolerance spinbox enabled only for `size_mtime`, exclusion list, symlink
  checkbox, Save / Save As. Missing paths shown in red with "path not found" and planning
  refused.
- `plan_view.py` — `ttk.Treeview` grouped by action kind, sortable columns
  (Action/Side/Path/Type/Size/Reason), filter box, checkbox column, colour tags for
  conflicts and deletes, summary bar, "Compared by: …" label, explicit "Nothing to do"
  state, and the first-run notice in two-way mode.
- `runner.py` — worker `threading.Thread` + `queue.Queue` + `root.after(100, drain)`;
  **no Tk calls off the main thread**; `threading.Event` cancel; indeterminate bar with a
  live "scanned N items" during preview; determinate bar (bytes for copies, count for
  deletes) during execution; confirm dialog naming the destructive part; summary with
  counts, elapsed, expandable errors and "Save log…"; then `last_run_utc`, `last_result`
  and (two-way, error-free) `snapshot` are written back.

*Test:* one smoke test that builds and destroys the main window, skipped when there is no
display.

## 4. Tooling

`pyproject.toml`: `requires-python = ">=3.11"`, no runtime dependencies,
`[project.scripts] dirsynk = "dirsynk.cli:main"`, `[project.optional-dependencies] dev =
["pytest", "ruff"]`, ruff config (line length 100, target py311, lint rules E/F/I/UP/B).
A local `.venv` holds pytest+ruff; the app itself is import-clean on a bare interpreter.

## 5. Notes from the build *(as built)*

Things decided while implementing that are not visible from the module list:

- **`core/validate.py`** was added as planned, and `path_problem()` is reused by the job
  editor to show a bad folder in red as you type, before anything else is attempted.
- **A kind clash decides its whole subtree.** When one side has a folder where the other
  has a file, the conflict row replaces the subtree, so the planner drops separate rows for
  the contents — they would only fail on execution. The executor also treats a delete whose
  target has already gone as *skipped* rather than failed.
- **Copies are chunked rather than a bare `shutil.copy2`.** The behaviour is copy2's — the
  bytes and then `copystat`, onto a `.dirsynk.tmp` sibling, then `os.replace` — done a
  megabyte at a time so a long copy can report progress and stop when Cancel is pressed.
- **A symlink is recreated, not copied through**, and two symlinks are equal when their
  targets are.
- **The plan carries the fingerprint of the settings it was built from.** Run refuses if
  the job has changed since, which covers the criterion and every other setting too.
- **Two progress bars during a run** — bytes for copies, count for items — rather than one
  bar switching meaning half way through.
- **The snapshot is rebuilt by rescanning both sides after a successful two-way run** and
  recording what both sides then agree on. An item whose copy failed is therefore absent
  from it, and will be retried next run.
- `DIRSYNK_HOME` overrides `~/.dirsynk`, which is what the tests use to stay out of the
  real home directory.

## 6. Definition of done — tracked

- [x] `python -m dirsynk` opens the app with no pip installs — verified on a bare
      interpreter with nothing installed
- [x] Job create / save / close / reopen / re-run from `~/.dirsynk`
- [x] Empty dirs created and removed in both modes
- [x] Same size, different mtime → copied under `size_mtime`, no action under `size_only`
- [x] `.deleted` reproduces the relative path and never overwrites
- [x] No write of any kind before the confirmation click — the preview path reads only
- [x] 50k-file tree previews without freezing (50,500 entries scanned and planned in 2.8 s
      on a worker thread, feeding ~200 progress updates to the UI); Cancel takes effect in
      well under a tenth of a second
- [x] Read-only destination file → reported error, not a crash
- [x] `pytest` green (132 tests), `ruff check` clean
