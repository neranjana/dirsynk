from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dirsynk.core import jobs  # noqa: E402  (after the path is set up)


@pytest.fixture(autouse=True)
def isolated_jobs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """No test may touch the real ``~/.dirsynk``.

    Runs write their log there, so every test that executes a plan would otherwise
    litter the home directory of whoever ran the suite. Modules that want to look
    inside the directory override this with a fixture of their own.
    """
    home = tmp_path / "dirsynk-home"
    monkeypatch.setenv(jobs.HOME_ENV_VAR, str(home))
    return home
