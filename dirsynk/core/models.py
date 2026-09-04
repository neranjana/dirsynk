"""Data model shared by every part of dirsynk.

Pure data and formatting helpers only: this module (like the rest of ``dirsynk.core``)
imports nothing from the UI and nothing from ``tkinter``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal

# --- vocabularies -----------------------------------------------------------------

EntryKind = Literal["file", "dir", "symlink"]
ActionKind = Literal[
    "mkdir", "copy_new", "copy_update", "delete_file", "delete_dir", "conflict", "skip"
]
Direction = Literal["a_to_b", "b_to_a"]
CompareMode = Literal["size_mtime", "size_only", "content"]
SyncMode = Literal["mirror", "two_way"]
DeletionPolicy = Literal["permanent", "quarantine"]

COMPARE_MODES: tuple[CompareMode, ...] = ("size_mtime", "size_only", "content")
SYNC_MODES: tuple[SyncMode, ...] = ("mirror", "two_way")
DELETION_POLICIES: tuple[DeletionPolicy, ...] = ("permanent", "quarantine")

#: Directories dirsynk owns; never scanned, never synced, never a source or target.
RESERVED_DIRS: tuple[str, ...] = (".deleted", ".dirsynk")

#: Suffix for the sibling temp file a copy is written to before ``os.replace``.
TEMP_SUFFIX = ".dirsynk.tmp"

DEFAULT_EXCLUDES: tuple[str, ...] = ("*.tmp", ".DS_Store", "Thumbs.db", "__pycache__/")

#: Upper bound on the pause between file copies. An hour between two files is already
#: far past anything useful; beyond it a job file is more likely wrong than deliberate.
COPY_PAUSE_LIMIT_S = 3600.0

SCHEMA_VERSION = 1

#: Order actions are executed (and grouped) in: parents before children for mkdir,
#: copies next, then file deletes, then directory deletes deepest-first.
ACTION_ORDER: dict[str, int] = {
    "mkdir": 0,
    "copy_new": 1,
    "copy_update": 2,
    "conflict": 3,
    "delete_file": 4,
    "delete_dir": 5,
    "skip": 6,
}

COMPARE_LABELS: dict[str, str] = {
    "size_mtime": "size and timestamp",
    "size_only": "size only",
    "content": "size and contents (SHA-256)",
}


# --- formatting helpers -----------------------------------------------------------


def human_bytes(n: int) -> str:
    """Render a byte count the way the plan and summary bar show it ("2.3 GB")."""
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            if abs(value) >= 100:
                return f"{value:.0f} {unit}"
            return f"{value:.1f} {unit}".replace(".0 ", " ")
        value /= 1024.0
    return f"{value:.1f} TB"


def human_duration(seconds: float) -> str:
    """Render an mtime gap the way reason strings show it ("3.2s", "52m", "1.4d")."""
    seconds = abs(seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def pause_summary(seconds: float, n_copies: int) -> str | None:
    """How a pause between copies reads to the user, or None when there is no pause.

    The estimate counts the gaps rather than the copies — a pause goes *between* two
    files, so ten copies wait nine times — and it is the pausing alone: the copying
    itself takes however long it takes on top.
    """
    if seconds <= 0:
        return None
    gaps = max(0, n_copies - 1)
    text = f"{seconds:g}s between file copies"
    return text if not gaps else f"{text} (adds about {human_duration(seconds * gaps)})"


# --- entries ----------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One item found by the scanner, relative to the root it was scanned from."""

    rel: PurePosixPath
    kind: EntryKind
    size: int  # 0 for dirs
    mtime_ns: int
    target: str | None = None  # symlink target, when kind == "symlink"

    @property
    def depth(self) -> int:
        return len(self.rel.parts)


# --- actions and plans ------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """A single copy, delete or directory creation the plan intends to perform."""

    kind: ActionKind
    rel: PurePosixPath
    direction: Direction
    reason: str
    size: int
    src: Path | None
    dst: Path | None
    included: bool = True

    @property
    def side(self) -> str:
        """Which root is written to, in the words the plan view uses."""
        written_to = "B" if self.direction == "a_to_b" else "A"
        if self.kind in ("delete_file", "delete_dir"):
            return f"in {written_to}"
        return "A → B" if self.direction == "a_to_b" else "B → A"

    @property
    def is_copy(self) -> bool:
        return self.kind in ("copy_new", "copy_update", "conflict")

    @property
    def is_delete(self) -> bool:
        return self.kind in ("delete_file", "delete_dir")


@dataclass
class Plan:
    """Everything a run would do, plus the context needed to explain it."""

    actions: list[Action] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    compare: CompareMode = "size_mtime"
    mode: SyncMode = "mirror"
    deletion_policy: DeletionPolicy = "quarantine"
    #: Two-way run with no stored snapshot: union copy, delete nothing.
    first_run: bool = False
    scanned_a: int = 0
    scanned_b: int = 0
    #: Fingerprint of the JobConfig this plan was built from; a plan must never be
    #: executed against settings that have changed since.
    config_fingerprint: str = ""

    @property
    def included(self) -> list[Action]:
        return [a for a in self.actions if a.included]

    def set_included(self, index: int, included: bool) -> None:
        self.actions[index] = replace(self.actions[index], included=included)

    def counts_by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for action in self.included:
            counts[action.kind] = counts.get(action.kind, 0) + 1
        return counts

    @property
    def bytes_to_copy(self) -> int:
        return sum(a.size for a in self.included if a.is_copy)

    @property
    def bytes_to_delete(self) -> int:
        return sum(a.size for a in self.included if a.is_delete)

    @property
    def n_copies(self) -> int:
        return sum(1 for a in self.included if a.is_copy)

    @property
    def n_deletes(self) -> int:
        return sum(1 for a in self.included if a.is_delete)

    @property
    def n_conflicts(self) -> int:
        return sum(1 for a in self.included if a.kind == "conflict")

    @property
    def n_mkdirs(self) -> int:
        return sum(1 for a in self.included if a.kind == "mkdir")

    @property
    def has_work(self) -> bool:
        return bool(self.included)

    def summary_line(self) -> str:
        """The summary bar text: '142 to copy (2.3 GB) · 8 to delete (110 MB) · 3 conflicts'."""
        if not self.has_work:
            return "Nothing to do"
        parts = [f"{self.n_copies} to copy ({human_bytes(self.bytes_to_copy)})"]
        if self.n_mkdirs:
            parts.append(f"{self.n_mkdirs} folder{'s' if self.n_mkdirs != 1 else ''} to create")
        if self.n_deletes:
            parts.append(f"{self.n_deletes} to delete ({human_bytes(self.bytes_to_delete)})")
        if self.n_conflicts:
            parts.append(f"{self.n_conflicts} conflict{'s' if self.n_conflicts != 1 else ''}")
        return " · ".join(parts)

    def compared_by(self) -> str:
        return f"Compared by: {COMPARE_LABELS[self.compare]}"

    def sorted_actions(self) -> list[Action]:
        return sorted(self.actions, key=lambda a: (ACTION_ORDER[a.kind], str(a.rel)))


# --- run results ------------------------------------------------------------------


@dataclass(frozen=True)
class ItemError:
    """A per-item failure; a run collects these and keeps going."""

    rel: str
    action: str
    message: str


@dataclass
class RunResult:
    copied: int = 0
    deleted: int = 0
    created: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_copied: int = 0
    elapsed_s: float = 0.0
    cancelled: bool = False
    errors: list[ItemError] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "copied": self.copied,
            "deleted": self.deleted,
            "created": self.created,
            "skipped": self.skipped,
            "failed": self.failed,
            "bytes_copied": self.bytes_copied,
            "elapsed_s": round(self.elapsed_s, 3),
            "cancelled": self.cancelled,
        }

    def summary_line(self) -> str:
        parts = [
            f"{self.copied} copied ({human_bytes(self.bytes_copied)})",
            f"{self.created} folders created",
            f"{self.deleted} deleted",
            f"{self.skipped} skipped",
            f"{self.failed} failed",
        ]
        tail = f" in {human_duration(self.elapsed_s)}"
        return " · ".join(parts) + tail + (" (cancelled)" if self.cancelled else "")


# --- job configuration ------------------------------------------------------------


@dataclass
class JobConfig:
    """A saved sync job, mirroring ``~/.dirsynk/<slug>.json`` one-for-one."""

    name: str
    path_a: str
    path_b: str
    mode: SyncMode = "mirror"
    deletion_policy: DeletionPolicy = "quarantine"
    compare: CompareMode = "size_mtime"
    mtime_tolerance_s: float = 2
    follow_symlinks: bool = False
    #: Seconds to wait between one file copy and the next; 0 copies without pausing.
    copy_pause_s: float = 0.0
    exclude: list[str] = field(default_factory=lambda: list(DEFAULT_EXCLUDES))
    created_utc: str = ""
    last_run_utc: str | None = None
    last_result: dict[str, Any] | None = None
    #: rel -> {"kind", "size", "mtime_ns"}, state after the last successful two-way run.
    snapshot: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Where this job was loaded from. Not serialised.
    file_path: Path | None = None

    @property
    def root_a(self) -> Path:
        return Path(self.path_a).expanduser()

    @property
    def root_b(self) -> Path:
        return Path(self.path_b).expanduser()

    def fingerprint(self) -> str:
        """Identity of every setting that can change a plan's meaning.

        ``copy_pause_s`` is deliberately absent: it paces a run without altering a
        single row of the plan, so changing it must not invalidate a plan you are
        already looking at.
        """
        return "|".join(
            [
                self.path_a,
                self.path_b,
                self.mode,
                self.deletion_policy,
                self.compare,
                str(self.mtime_tolerance_s),
                str(self.follow_symlinks),
                ",".join(self.exclude),
            ]
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "path_a": self.path_a,
            "path_b": self.path_b,
            "mode": self.mode,
            "deletion_policy": self.deletion_policy,
            "compare": self.compare,
            "mtime_tolerance_s": self.mtime_tolerance_s,
            "follow_symlinks": self.follow_symlinks,
            "copy_pause_s": self.copy_pause_s,
            "exclude": list(self.exclude),
            "created_utc": self.created_utc,
            "last_run_utc": self.last_run_utc,
            "last_result": self.last_result,
            "snapshot": self.snapshot,
        }
