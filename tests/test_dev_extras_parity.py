"""`pip install -e ".[dev]"` must install what CI installs.

Two lists describe the same thing from different sides: `requirements-test.txt`
is what the CI matrix installs, and the `dev` extra is what a human gets after
a clone. A pin added to one and forgotten in the other produces the worst kind
of green build — the suite passes in CI and fails on the machine of whoever
next tries to run it, or vice versa.

The `dev` extra deliberately holds no version copies: it self-references
`singularity[imgqa,secrets]` so the base and extra pins stay declared exactly
once. This test checks the resulting PACKAGE SET, which is the thing that has
to agree.
"""
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict:
    try:
        import tomllib
    except ImportError:                       # Python 3.10
        tomllib = pytest.importorskip(
            "tomli", reason="needs tomllib (3.11+) or tomli to read pyproject")
    return tomllib.loads(REPO.joinpath("pyproject.toml").read_text(encoding="utf-8"))


def _name(requirement: str) -> str:
    """`Pillow>=10` -> `pillow`; extras and markers dropped."""
    base = re.split(r"[<>=!~;\[]", requirement, maxsplit=1)[0]
    return base.strip().lower().replace("_", "-")


def _requirements_test_txt() -> set[str]:
    out = set()
    for line in REPO.joinpath("requirements-test.txt").read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.add(_name(line))
    return out


def _dev_extra_packages() -> set[str]:
    """The `dev` extra, with its self-reference to this project's own extras
    expanded — that indirection is the point, so the test has to follow it."""
    proj = _load_pyproject()["project"]
    extras = proj["optional-dependencies"]
    out = {_name(r) for r in proj["dependencies"]}
    for req in extras["dev"]:
        if _name(req) == "singularity":
            named = re.search(r"\[([^\]]+)\]", req)
            assert named, "a self-reference without extras installs nothing extra"
            for extra in named.group(1).split(","):
                out |= {_name(r) for r in extras[extra.strip()]}
        else:
            out.add(_name(req))
    return out


def test_dev_extra_matches_ci_requirements():
    assert _dev_extra_packages() == _requirements_test_txt()


def test_dev_extra_brings_pytest():
    """The whole reason the group exists: a fresh clone could not run the suite."""
    assert "pytest" in _dev_extra_packages()


def test_dev_extra_stays_light():
    """No torch: every test uses a fake encoder, and `requirements-test.txt`
    says so in its own header. Pulling the ML stack in here would make the
    documented developer setup an order of magnitude heavier than CI's."""
    assert "sentence-transformers" not in _dev_extra_packages()


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_self_reference_uses_the_real_project_name():
    """`pip install -e ".[dev]"` resolves the self-reference by distribution
    name — a rename here fails silently, installing only pytest."""
    proj = _load_pyproject()["project"]
    assert any(_name(r) == _name(proj["name"])
               for r in proj["optional-dependencies"]["dev"])
