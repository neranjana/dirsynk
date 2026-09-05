# dirsynk

A small desktop app that synchronises two folders — and shows you exactly what it intends
to do before it does any of it. Every copy and every delete appears in a plan you can read,
filter and untick, and nothing is written until you confirm.

Python 3.11+, standard library only. The GUI is `tkinter`; there is nothing to `pip install`
to run it.

```
python -m dirsynk
```

## Install

Running it needs no installation at all — clone the repository and run
`python -m dirsynk` from inside it.

To get a `dirsynk` command on your PATH:

```
pip install .
dirsynk            # the GUI
dirsynk --list     # the CLI
```

For development:

```
pip install -e ".[dev]"
pytest
ruff check .
```

## The two modes

**Mirror (A → B).** Folder A is the source of truth and folder B is made to match it.
Anything new in A is copied to B, anything that differs is copied over B's version, and
anything in B that is not in A is removed. Use it for backups and for pushing a
known-good folder somewhere else.

**Two-way.** Both folders are equals: a change on either side travels to the other.
To know whether a file that is missing from one side was *deleted there* or *added on the
other*, dirsynk remembers the state of both folders after each successful run — a snapshot
stored inside the job file. On the very first run there is no snapshot yet, so it does the
only safe thing: it copies everything both ways and deletes nothing. The plan says so
plainly when that is what is about to happen.

If both sides changed the same file since the last run, that row is a **conflict**. The
default resolution is that the newer file wins, but the row is highlighted and you can
untick it to leave both sides alone and sort it out yourself.

## The three comparison criteria

How "the same file" is decided is your choice, per job, in the job editor.

| Criterion | Two files differ when | Pick it for |
|---|---|---|
| **Size and timestamp** (default) | their size differs, or their modified time differs by more than the tolerance (2 s by default) | almost everything |
| **Size only** | their size differs — timestamps are ignored entirely | archives, and destinations that do not keep timestamps faithfully: some SMB shares, some cloud mounts, anything freshly unzipped |
| **Size and contents (SHA-256)** | their size differs, or the sizes match and their contents differ | when you need to be certain |

Two things worth knowing:

- **"Size only" cannot see an edit that does not change the file's size.** Fix a typo in a
  text file without changing its length, and this criterion will not notice. That is the
  trade-off you accept in exchange for it being the cheapest and for it ignoring
  timestamps that a filesystem may have mangled.
- **"Size and contents" only hashes when the sizes already match** — a size mismatch is
  already conclusive, so no bytes are read in that case. Within one plan no file is ever
  hashed twice.

The timestamp tolerance applies only to "size and timestamp"; the other two ignore it. It
exists because FAT and SMB store timestamps coarsely, and without it a file could look
changed every single run.

Whichever you pick, the criterion decides only *whether* two files differ. When two
folders both have a version and they differ, the newer one wins — under every criterion —
because a difference still has to be resolved to a direction. That is why a plan row can
read "sizes differ (4 KB vs 8 KB), A newer by 52m".

The plan always says which criterion produced it ("Compared by: size only"), and changing
the criterion invalidates the plan: you have to preview again before you can run.

## Pausing between copies

A job can wait a fixed number of seconds between one file copy and the next —
**Pause between file copies** in the job editor, `copy_pause_s` in the job file, `0` for
no pause at all. Set it to 5 and dirsynk copies a file, waits five seconds, copies the
next, and so on. It is there for the destinations that punish being hammered: a rate-limited
cloud mount, a NAS you would rather not saturate, a drive you want to keep responsive for
something else while a long sync runs in the background.

Three things it deliberately does not do:

- **The pause goes *between* copies, not around them.** Ten files wait nine times: nothing
  before the first copy, nothing after the last.
- **It slows copies only.** Creating folders and deleting are not spaced out — those are
  cheap, and pausing between them would only make a run longer for no benefit.
- **It never blocks Cancel.** The wait is on the cancel signal itself, so a run pausing a
  minute between files still stops the moment you ask it to, rather than a minute later.

The plan says what the pausing will cost before you commit to it — "Pausing 5s between
file copies (adds about 12m)" — counting the gaps, not the files, and counting only the
waiting: the copying takes however long it takes on top of that.

## Where things live

**Jobs** are JSON files in `~/.dirsynk/`, one per job, written atomically. They are
readable and editable by hand:

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
  "copy_pause_s": 0,
  "exclude": ["*.tmp", ".DS_Store", "Thumbs.db", "__pycache__/"],
  "created_utc": "2026-09-04T00:00:00Z",
  "last_run_utc": null,
  "last_result": null,
  "snapshot": {}
}
```

A file with an unknown `schema_version`, or a `compare` value that is not one of the three,
is rejected with a message saying so rather than being guessed at. A job whose folders have
since disappeared still opens — the paths are shown in red and planning is refused until
you fix them.

**Run logs** are written to `~/.dirsynk` too, one per run, named after the job file and
the moment the run started — `photos-to-nas-20260905-142530.log`, carrying the same stamp
as that run's `.deleted` folder, so a log and the files it quarantined name each other.
Every folder created, file copied and item deleted gets a line, with both full paths and
what became of it:

```
# dirsynk run — Photos to NAS — started 2026-09-05 14:25:30
# A: /Users/me/Photos
# B: /Volumes/nas/Photos
# Mode: mirror · Compare: size_mtime · Deletions: quarantine
2026-09-05 14:25:30  create dir  /Users/me/Photos/2019  ->  /Volumes/nas/Photos/2019  success
2026-09-05 14:25:31  copy        /Users/me/Photos/a.jpg  ->  /Volumes/nas/Photos/a.jpg  success
2026-09-05 14:25:31  copy        /Users/me/Photos/b.jpg  ->  /Volumes/nas/Photos/b.jpg  failed: Permission denied
2026-09-05 14:25:32  delete      /Volumes/nas/Photos/old.jpg  ->  /Volumes/nas/Photos/.deleted/20260905-142530/old.jpg  success
# finished 2026-09-05 14:25:32 — 4 operations, 3 succeeded, 1 failed
```

A delete records where the item went: its place under `.deleted`, or `-` when the job
deletes permanently and there is nowhere for it to go.

Each line is written in two halves. The operation and its two paths go down *before* the
work starts and are flushed immediately; the outcome — `success`, or `failed:` and the
error — is appended to that same line when the work finishes. So a run that is killed
mid-copy leaves one line with no status on the end, naming exactly the operation that was
in flight. That is the one thing a log written afterwards could never tell you.

Nothing about the log can cost you a sync: if it cannot be written, the run says why and
copies your files anyway. Nothing prunes old logs either, for the same reason nothing
empties `.deleted`: they are the record of what happened to your files, and deciding they
have stopped mattering is your call, not the program's.

**`.deleted`** is where deleted items go, unless you choose permanent deletion. Each folder
gets its own, at its root, with the original relative path preserved under a per-run
timestamp:

```
/Volumes/nas/Photos/.deleted/20260904-142530/2019/summer/IMG_0421.jpg
```

Names never collide destructively — a second item with the same name becomes `IMG_0421.jpg (2)`.

**Nothing ever empties `.deleted` for you.** There is no retention policy and no automatic
pruning, by design: the point of quarantine is that it is still there when you realise you
needed the file. Delete those folders yourself when you are satisfied. `.deleted` and
`.dirsynk` are always excluded from scanning and can never be used as a sync folder.

## The command line

The same engine, for scripting and for cron:

```
dirsynk --list
dirsynk --job "Photos to NAS" --dry-run
dirsynk --job "Photos to NAS" --compare size_only --dry-run
dirsynk --job "Photos to NAS" --pause 5 --yes
dirsynk --job "Photos to NAS" --yes
```

- `--dry-run` prints the plan and exits, writing nothing.
- `--yes` skips the confirmation prompt (use it in scheduled runs).
- `--compare` overrides the job's saved criterion **for that run only**; it is never
  written back to the job file.
- `--pause` does the same for the pause between file copies — `--pause 0` runs a paced
  job at full speed once, without editing the job.

Exit codes: `0` fine, `1` the job could not be run at all, `3` the run finished but some
items failed, `130` cancelled or declined.

## Exclusions

`fnmatch` glob patterns, matched against a file's path relative to the folder root or
against its bare name — so `.DS_Store` catches it at any depth. A trailing slash
(`__pycache__/`) means a folder and everything inside it.

## What it will not do

Before planning, dirsynk refuses, with a message naming the reason, when: the two folders
are the same folder; one is inside the other; a folder is missing, is not a folder, or
cannot be read; the destination cannot be written to; or a path lies inside a `.deleted`
or `.dirsynk` folder.

During a run, a failure on one item never stops the rest — it is recorded against that item
and reported at the end, with the whole log saveable to a file.

Symbolic links are not followed by default: they are synced as links. Turn on
"follow symbolic links" and they are traversed, with cycles detected. Special files —
sockets, fifos, devices — are skipped.

## How it is put together

```
dirsynk/
  core/        the engine: models, scanner, planner, deleter, executor, jobs,
               runlog, validate
  ui/          tkinter: app, job_editor, plan_view, runner, branding
  ui/resources icon-512.png and its smaller siblings
```

The app calls itself **DirsyNK** and wears an icon of two folders inside a cycle of
arrows. Neither is free when you launch a script rather than a bundled application: macOS
takes the name in the menu bar and the icon in the Dock from the bundle that is running,
which is the Python framework, so an unadorned tkinter app is called "Python" and wears
the Python icon. `ui/branding.py` fixes both — it rewrites `CFBundleName` in the running
bundle's info dictionary before Tk starts, and hands the icon to `wm iconphoto` and to
`NSApplication` — through `ctypes`, so there is still nothing to install. Every step is
guarded: on a platform or a machine where one is unavailable, the app opens regardless.

The icon itself is drawn by `tools/make_icon.py`, which renders the shapes and writes the
PNGs with nothing but `zlib`. The PNGs are committed, so you only need to run it if you
want to change the icon.

`dirsynk.core` has no UI imports at all (there is a test that enforces it), is synchronous,
and drives both the GUI and the CLI. All scanning, planning and executing happens on a
worker thread that talks to the Tk main loop through a `queue.Queue`, so the window stays
responsive and long runs can be cancelled.
