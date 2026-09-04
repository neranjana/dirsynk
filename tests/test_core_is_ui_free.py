"""The core engine must be importable and testable with no display and no UI code."""

from __future__ import annotations

import ast
from pathlib import Path

CORE = Path(__file__).resolve().parent.parent / "dirsynk" / "core"
FORBIDDEN = ("tkinter", "dirsynk.ui")


def _imported_modules(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_core_never_imports_the_ui() -> None:
    for path in sorted(CORE.glob("*.py")):
        for name in _imported_modules(path.read_text(encoding="utf-8")):
            for banned in FORBIDDEN:
                assert not (name == banned or name.startswith(banned + ".")), (
                    f"{path.name} imports {name}"
                )
