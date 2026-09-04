from __future__ import annotations

from pathlib import Path, PurePosixPath

from dirsynk.core.models import Action, Plan, human_bytes, human_duration


def _copy(rel: str, size: int, kind: str = "copy_new") -> Action:
    return Action(
        kind=kind,  # type: ignore[arg-type]
        rel=PurePosixPath(rel),
        direction="a_to_b",
        reason="new in A",
        size=size,
        src=Path("/a") / rel,
        dst=Path("/b") / rel,
    )


def test_human_bytes_matches_the_wording_used_in_reasons() -> None:
    assert human_bytes(4096) == "4 KB"
    assert human_bytes(8192) == "8 KB"
    assert human_bytes(110 * 1024**2) == "110 MB"
    assert human_bytes(0) == "0 B"


def test_human_duration_matches_the_wording_used_in_reasons() -> None:
    assert human_duration(3.2) == "3.2s"
    assert human_duration(52 * 60) == "52m"


def test_empty_plan_says_nothing_to_do() -> None:
    assert Plan().summary_line() == "Nothing to do"
    assert not Plan().has_work


def test_summary_counts_only_included_rows() -> None:
    plan = Plan(actions=[_copy("a.txt", 1024), _copy("b.txt", 1024)])
    assert plan.n_copies == 2
    plan.set_included(0, False)
    assert plan.n_copies == 1
    assert plan.bytes_to_copy == 1024


def test_summary_line_lists_copies_deletes_and_conflicts() -> None:
    plan = Plan(
        actions=[
            _copy("a.txt", 2 * 1024**3),
            Action(
                kind="delete_file",
                rel=PurePosixPath("gone.txt"),
                direction="a_to_b",
                reason="removed from A",
                size=110 * 1024**2,
                src=None,
                dst=Path("/b/gone.txt"),
            ),
            _copy("c.txt", 0, kind="conflict"),
        ]
    )
    line = plan.summary_line()
    assert "1 to delete (110 MB)" in line
    assert "1 conflict" in line
    assert "2 to copy" in line


def test_compared_by_names_the_active_criterion() -> None:
    assert Plan(compare="size_only").compared_by() == "Compared by: size only"
