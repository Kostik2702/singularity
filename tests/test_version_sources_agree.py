"""The two places that state the version must state the same one.

This exists because v0.12.0 was tagged with them disagreeing: `pyproject.toml`
said 0.12.0 and `singularity/__init__.py` still said 0.11.2. Nothing caught it —
the suite was green, the release was pushed, and the constant was only bumped
afterwards while taking the baseline.

The failure mode is quiet and expensive in the same way a stale daemon is: a
build that reports one version while the package metadata reports another turns
every later "is this the fixed code?" into a guess. The vault already carries
the sibling lesson — package metadata is not proof of an upgrade — and this is
the same class from the other end: the code was not proof of the release.

Read from disk rather than through `importlib.metadata`, on purpose. Metadata
comes from whatever was last installed, which is exactly the thing that lies
after a `git pull`; the two files in the tree are what a tag actually carries.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "pyproject.toml has no top-level version"
    return match.group(1)


def _package_version() -> str:
    text = (REPO / "singularity" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert match, "singularity/__init__.py has no __version__"
    return match.group(1)


def test_pyproject_and_package_agree() -> None:
    """One release, one number.

    If this fails during a release, bump the one that lagged — do not "fix" it by
    editing the test. The number a user sees (`mb --version`) comes from the
    constant; the number pip and the wheel see comes from pyproject; a release
    where those differ is two releases wearing one tag.
    """
    assert _package_version() == _pyproject_version(), (
        f"singularity/__init__.py says {_package_version()}, "
        f"pyproject.toml says {_pyproject_version()} — a tag taken here would "
        f"carry two versions"
    )


def test_the_version_is_a_release_number() -> None:
    """Guards the shape, so a typo cannot pass the agreement check by matching it."""
    assert re.fullmatch(r"\d+\.\d+\.\d+", _package_version()), _package_version()


def test_nothing_claims_a_version_that_does_not_exist_yet():
    """`pyproject` and `__init__` agreeing says nothing about PROSE.

    Four docstrings were written on 2026-08-08 saying "changed in v0.17.0" while
    the repo stood at 0.16.0 and the CHANGELOG had no such entry — a sentence
    that reads as authoritative and is unverifiable, and that becomes false if
    the work ships as 0.16.1 instead. Dates are facts and need no release; a
    version number is a promise about one.

    Scoped to THIS project's major series. The first version of the check
    matched any `vX.Y.Z` and flagged the Node versions in the CLI-locator
    fixtures — a check that cries wolf is a check somebody switches off
    (`redact.py` says the same about its own placeholder list). Comparing majors
    follows the project automatically if it ever leaves 0.x.
    """
    import re
    import subprocess
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1]
    current = tuple(int(p) for p in _pyproject_version().split("."))
    pattern = re.compile(r"\bv(\d+)\.(\d+)\.(\d+)\b")

    tracked = subprocess.run(["git", "ls-files"], cwd=engine, capture_output=True,
                             text=True, check=True).stdout.split("\n")
    ahead: dict[str, set[str]] = {}
    for rel in tracked:
        if not rel or not rel.endswith((".py", ".md")):
            continue
        if rel == f"tests/{Path(__file__).name}":
            continue                     # this file names the example it forbids
        try:
            text = (engine / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        future = {m.group(0) for m in pattern.finditer(text)
                  if int(m.group(1)) == current[0]
                  and tuple(int(g) for g in m.groups()) > current}
        if future:
            ahead[rel] = future

    assert ahead == {}, (
        f"these name a version later than {'.'.join(map(str, current))}, which "
        f"has not shipped:\n  "
        + "\n  ".join(f"{rel}: {', '.join(sorted(v))}" for rel, v in sorted(ahead.items()))
        + "\nUse the date instead — it is true without waiting for a release.")
