"""`ruff check .` must stay at zero.

A lint config that nobody enforces becomes a backlog: the count creeps up, the
output stops being read, and the rules that do catch real defects (an import
that stopped being used, a name bound and forgotten, an `except` that loses its
cause) drown in the noise. The rule set in `pyproject.toml` was chosen to be
reachable, so the gate is simply "zero".

Deliberately shells out to the same command a human types instead of importing
ruff's API: what has to hold is that `ruff check .` passes in this checkout.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _ruff() -> list[str] | None:
    """Prefer the interpreter's own ruff so the gate grades this venv."""
    for candidate in ([sys.executable, "-m", "ruff"], ["ruff"]):
        if candidate[0] == "ruff" and shutil.which("ruff") is None:
            continue
        probe = subprocess.run([*candidate, "--version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return candidate
    return None


def test_ruff_reports_nothing():
    cmd = _ruff()
    if cmd is None:
        pytest.skip("ruff not installed (it is in the dev extra and requirements-test.txt)")
    run = subprocess.run([*cmd, "check", "--output-format", "concise", "."],
                         cwd=str(REPO), capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, (
        "ruff found issues — fix them, or record the exemption in pyproject "
        f"with the reason:\n{run.stdout[-4000:]}")
