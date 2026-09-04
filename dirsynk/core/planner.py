"""Turn two scanned trees (and, in two-way mode, a snapshot) into a Plan.

Nothing here touches the filesystem except to read file bytes for the ``content``
criterion, and even that happens lazily and at most once per file per plan.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import Action, CompareMode, Entry, JobConfig, Plan, human_bytes, human_duration
from .scanner import ScanResult, scan

HASH_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes, read in chunks so large files stay cheap in memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class HashCache:
    """Per-plan digest cache, so no file is read twice while one plan is built."""

    hasher: Callable[[Path], str] = sha256_file
    _cache: dict[Path, str] = field(default_factory=dict)

    def digest(self, path: Path) -> str:
        cached = self._cache.get(path)
        if cached is None:
            cached = self.hasher(path)
            self._cache[path] = cached
        return cached


def _sizes_differ_reason(a: Entry, b: Entry) -> str:
    return f"sizes differ ({human_bytes(a.size)} vs {human_bytes(b.size)})"


def _newer_reason(a: Entry, b: Entry) -> str:
    delta = human_duration(abs(a.mtime_ns - b.mtime_ns) / 1_000_000_000)
    return f"A newer by {delta}" if a.mtime_ns > b.mtime_ns else f"B newer by {delta}"


def entries_differ(
    a: Entry,
    b: Entry,
    *,
    compare: CompareMode,
    mtime_tolerance_s: float,
    path_a: Path,
    path_b: Path,
    hashes: HashCache,
) -> tuple[bool, str]:
    """Do these two items differ under the job's criterion, and what says so?

    Directories are never subject to the criterion, and neither are symlinks — the
    criterion decides file-vs-file equality only.
    """
    if a.kind == "dir" and b.kind == "dir":
        return False, ""
    if a.kind == "symlink" or b.kind == "symlink":
        if a.kind != b.kind:
            return True, _kind_mismatch_reason(a, b)
        if a.target != b.target:
            return True, "link target differs"
        return False, ""
    if a.kind != b.kind:
        return True, _kind_mismatch_reason(a, b)

    if a.size != b.size:
        return True, _sizes_differ_reason(a, b)

    # Sizes match from here on; what settles it depends on the criterion.
    if compare == "size_only":
        return False, ""
    if compare == "content":
        if hashes.digest(path_a) != hashes.digest(path_b):
            return True, "same size, contents differ"
        return False, ""

    tolerance_ns = int(mtime_tolerance_s * 1_000_000_000)
    if abs(a.mtime_ns - b.mtime_ns) > tolerance_ns:
        return True, _newer_reason(a, b)
    return False, ""


def _kind_mismatch_reason(a: Entry, b: Entry) -> str:
    words = {"dir": "folder", "file": "file", "symlink": "link"}
    return f"{words[a.kind]} in A, {words[b.kind]} in B"


def changed_since_snapshot(
    entry: Entry, snap: Mapping[str, object] | None, mtime_tolerance_s: float
) -> bool:
    """Has this side been edited since the last successful run?

    Always size-or-mtime, whatever the job's criterion is: the snapshot exists to spot
    edits since the last run, not to decide equality between the two sides, which is why
    its schema never has to carry a hash.
    """
    if snap is None:
        return True
    if snap.get("kind") != entry.kind:
        return True
    if int(snap.get("size", -1)) != entry.size:  # type: ignore[arg-type]
        return True
    tolerance_ns = int(mtime_tolerance_s * 1_000_000_000)
    return abs(int(snap.get("mtime_ns", 0)) - entry.mtime_ns) > tolerance_ns  # type: ignore[arg-type]


def build_snapshot(
    entries_a: Mapping[PurePosixPath, Entry], entries_b: Mapping[PurePosixPath, Entry]
) -> dict[str, dict[str, object]]:
    """The state to remember after a two-way run: what both sides now agree on."""
    snapshot: dict[str, dict[str, object]] = {}
    for rel, entry_a in entries_a.items():
        entry_b = entries_b.get(rel)
        if entry_b is None or entry_b.kind != entry_a.kind:
            continue
        snapshot[str(rel)] = {
            "kind": entry_a.kind,
            "size": entry_a.size,
            "mtime_ns": entry_a.mtime_ns,
        }
    return snapshot


# --- plan construction ------------------------------------------------------------


def _copy_action(
    entry: Entry,
    *,
    kind: str,
    direction: str,
    reason: str,
    root_src: Path,
    root_dst: Path,
) -> Action:
    rel_parts = entry.rel.parts
    # A directory that simply has to exist on the other side is an mkdir; a conflict
    # stays a conflict, because the user has to look at it.
    effective = "mkdir" if entry.kind == "dir" and kind != "conflict" else kind
    return Action(
        kind=effective,  # type: ignore[arg-type]
        rel=entry.rel,
        direction=direction,  # type: ignore[arg-type]
        reason=reason,
        size=0 if entry.kind == "dir" else entry.size,
        src=root_src.joinpath(*rel_parts),
        dst=root_dst.joinpath(*rel_parts),
    )


def _delete_action(entry: Entry, *, direction: str, reason: str, root: Path) -> Action:
    return Action(
        kind="delete_dir" if entry.kind == "dir" else "delete_file",
        rel=entry.rel,
        direction=direction,  # type: ignore[arg-type]
        reason=reason,
        size=0 if entry.kind == "dir" else entry.size,
        src=None,
        dst=root.joinpath(*entry.rel.parts),
    )


def build_plan(
    scan_a: ScanResult,
    scan_b: ScanResult,
    config: JobConfig,
    *,
    hasher: Callable[[Path], str] | None = None,
    compare: CompareMode | None = None,
) -> Plan:
    """Compare two scans and produce every action a run would perform.

    ``compare`` overrides the job's saved criterion for this plan only; the JobConfig is
    never modified.
    """
    criterion: CompareMode = compare or config.compare
    root_a, root_b = config.root_a, config.root_b
    hashes = HashCache(hasher or sha256_file)
    plan = Plan(
        compare=criterion,
        mode=config.mode,
        deletion_policy=config.deletion_policy,
        scanned_a=len(scan_a.entries),
        scanned_b=len(scan_b.entries),
        errors=list(scan_a.errors) + list(scan_b.errors),
        config_fingerprint=config.fingerprint(),
    )

    entries_a, entries_b = scan_a.entries, scan_b.entries
    every_rel = sorted(set(entries_a) | set(entries_b), key=lambda rel: (len(rel.parts), str(rel)))

    if config.mode == "mirror":
        _plan_mirror(
            plan, every_rel, entries_a, entries_b, config, criterion, hashes, root_a, root_b
        )
    else:
        plan.first_run = not config.snapshot
        _plan_two_way(
            plan, every_rel, entries_a, entries_b, config, criterion, hashes, root_a, root_b
        )
    plan.actions = _drop_actions_under_conflicts(plan.actions)
    return plan


def _drop_actions_under_conflicts(actions: list[Action]) -> list[Action]:
    """A kind clash decides everything beneath it.

    When one side has a folder where the other has a file, resolving that one row replaces
    the whole subtree, so separate rows for its contents would only fail on execution.
    """
    conflicts = [action.rel for action in actions if action.kind == "conflict"]
    if not conflicts:
        return actions
    return [
        action
        for action in actions
        if not any(action.rel != rel and action.rel.is_relative_to(rel) for rel in conflicts)
    ]


def _plan_mirror(
    plan: Plan,
    every_rel: list[PurePosixPath],
    entries_a: Mapping[PurePosixPath, Entry],
    entries_b: Mapping[PurePosixPath, Entry],
    config: JobConfig,
    criterion: CompareMode,
    hashes: HashCache,
    root_a: Path,
    root_b: Path,
) -> None:
    """A is the source of truth: copy what B lacks, update what differs, delete the rest."""
    for rel in every_rel:
        entry_a, entry_b = entries_a.get(rel), entries_b.get(rel)
        if entry_a is not None and entry_b is None:
            plan.actions.append(
                _copy_action(
                    entry_a,
                    kind="copy_new",
                    direction="a_to_b",
                    reason="new in A",
                    root_src=root_a,
                    root_dst=root_b,
                )
            )
        elif entry_a is None and entry_b is not None:
            plan.actions.append(
                _delete_action(entry_b, direction="a_to_b", reason="not in A", root=root_b)
            )
        elif entry_a is not None and entry_b is not None:
            differ, reason = entries_differ(
                entry_a,
                entry_b,
                compare=criterion,
                mtime_tolerance_s=config.mtime_tolerance_s,
                path_a=root_a.joinpath(*rel.parts),
                path_b=root_b.joinpath(*rel.parts),
                hashes=hashes,
            )
            if not differ:
                continue
            if entry_a.kind != entry_b.kind:
                plan.actions.append(
                    _copy_action(
                        entry_a,
                        kind="conflict",
                        direction="a_to_b",
                        reason=reason,
                        root_src=root_a,
                        root_dst=root_b,
                    )
                )
                continue
            plan.actions.append(
                _copy_action(
                    entry_a,
                    kind="copy_update",
                    direction="a_to_b",
                    reason=reason,
                    root_src=root_a,
                    root_dst=root_b,
                )
            )


def _plan_two_way(
    plan: Plan,
    every_rel: list[PurePosixPath],
    entries_a: Mapping[PurePosixPath, Entry],
    entries_b: Mapping[PurePosixPath, Entry],
    config: JobConfig,
    criterion: CompareMode,
    hashes: HashCache,
    root_a: Path,
    root_b: Path,
) -> None:
    """Both sides may change; the snapshot says which side's change is the new one."""
    snapshot = config.snapshot

    for rel in every_rel:
        key = str(rel)
        snap = snapshot.get(key)
        entry_a, entry_b = entries_a.get(rel), entries_b.get(rel)

        if entry_a is not None and entry_b is not None:
            _plan_two_way_both_sides(
                plan, rel, entry_a, entry_b, snap, config, criterion, hashes, root_a, root_b
            )
            continue

        present, missing_side = (entry_a, "b") if entry_a is not None else (entry_b, "a")
        assert present is not None
        src_root, dst_root = (root_a, root_b) if missing_side == "b" else (root_b, root_a)
        direction = "a_to_b" if missing_side == "b" else "b_to_a"
        other = "B" if missing_side == "b" else "A"
        here = "A" if missing_side == "b" else "B"

        if snap is not None and not plan.first_run:
            # It was there at the last run and is gone from the other side now:
            # that side deleted it, so this side follows. Writing to this side.
            plan.actions.append(
                _delete_action(
                    present,
                    direction="b_to_a" if here == "A" else "a_to_b",
                    reason=f"removed from {other}",
                    root=src_root,
                )
            )
        else:
            plan.actions.append(
                _copy_action(
                    present,
                    kind="copy_new",
                    direction=direction,
                    reason=f"new in {here}",
                    root_src=src_root,
                    root_dst=dst_root,
                )
            )


def _plan_two_way_both_sides(
    plan: Plan,
    rel: PurePosixPath,
    entry_a: Entry,
    entry_b: Entry,
    snap: Mapping[str, object] | None,
    config: JobConfig,
    criterion: CompareMode,
    hashes: HashCache,
    root_a: Path,
    root_b: Path,
) -> None:
    differ, reason = entries_differ(
        entry_a,
        entry_b,
        compare=criterion,
        mtime_tolerance_s=config.mtime_tolerance_s,
        path_a=root_a.joinpath(*rel.parts),
        path_b=root_b.joinpath(*rel.parts),
        hashes=hashes,
    )
    if not differ:
        return

    # A difference still has to be resolved to a direction, and under every criterion the
    # newer mtime is the one that wins.
    a_wins = entry_a.mtime_ns >= entry_b.mtime_ns
    direction = "a_to_b" if a_wins else "b_to_a"
    winner = entry_a if a_wins else entry_b
    src_root, dst_root = (root_a, root_b) if a_wins else (root_b, root_a)

    # Say which side won and by how much, unless the reason already said it
    # ("sizes differ (4 KB vs 8 KB), A newer by 52m").
    if "newer by" not in reason:
        if entry_a.mtime_ns == entry_b.mtime_ns:
            reason = f"{reason}, same timestamp — A wins"
        else:
            delta = human_duration(abs(entry_a.mtime_ns - entry_b.mtime_ns) / 1_000_000_000)
            reason = f"{reason}, {'A' if a_wins else 'B'} newer by {delta}"

    changed_a = changed_since_snapshot(entry_a, snap, config.mtime_tolerance_s)
    changed_b = changed_since_snapshot(entry_b, snap, config.mtime_tolerance_s)
    kind_mismatch = entry_a.kind != entry_b.kind
    both_changed = snap is not None and changed_a and changed_b

    if kind_mismatch or both_changed:
        prefix = "both sides changed since last sync; " if both_changed else ""
        plan.actions.append(
            _copy_action(
                winner,
                kind="conflict",
                direction=direction,
                reason=f"{prefix}{reason}",
                root_src=src_root,
                root_dst=dst_root,
            )
        )
        return

    plan.actions.append(
        _copy_action(
            winner,
            kind="copy_update",
            direction=direction,
            reason=reason,
            root_src=src_root,
            root_dst=dst_root,
        )
    )


# --- convenience ------------------------------------------------------------------


def preview(
    config: JobConfig,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
    compare: CompareMode | None = None,
    hasher: Callable[[Path], str] | None = None,
) -> tuple[Plan, ScanResult, ScanResult]:
    """Scan both roots and build the plan. Reads only; writes nothing, ever."""
    scanned_a = 0

    def progress_a(n: int) -> None:
        nonlocal scanned_a
        scanned_a = n
        if progress:
            progress(n)

    def progress_b(n: int) -> None:
        if progress:
            progress(scanned_a + n)

    scan_a = scan(
        config.root_a,
        exclude=config.exclude,
        follow_symlinks=config.follow_symlinks,
        cancel=cancel,
        progress=progress_a,
    )
    scan_b = scan(
        config.root_b,
        exclude=config.exclude,
        follow_symlinks=config.follow_symlinks,
        cancel=cancel,
        progress=progress_b,
    )
    plan = build_plan(scan_a, scan_b, config, hasher=hasher, compare=compare)
    return plan, scan_a, scan_b
