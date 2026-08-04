"""The bin/ shims must survive a CWD that contains a `singularity/` directory
(issue #16) — which is exactly the parent directory of a standard clone.

`python -m singularity` puts the CWD first on `sys.path`; Python then adopts
`<cwd>/singularity` as a namespace package and it shadows the installed one:

    ImportError: cannot import name '__version__' from 'singularity'
        (unknown location)

The unit-level guard for doctor's D6 lives in test_doctor.py with `_run_shim`
stubbed. This file runs the real thing, because the failure is a property of
the launcher mechanism, not of the check that reports it.
"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
LAUNCH = ENGINE / "bin" / "_launch.py"
SHIMS = {
    "mb": "singularity.cli:main",
    "imgqa": "singularity.imgqa.cli:main",
    "design-scout": "singularity.scout.cli:main",
}


@pytest.fixture
def shadowing_cwd(tmp_path):
    """A directory that holds a `singularity/` folder, like ~/projects does."""
    (tmp_path / "singularity").mkdir()
    return tmp_path


def test_no_shim_launches_via_dash_m():
    """`-m` is the mechanism that puts the CWD on sys.path. Grepping for it is
    cheap and catches a well-meant revert that the runtime tests would only
    catch on a machine where the console scripts happen to be missing."""
    for name in SHIMS:
        for shim in (ENGINE / "bin" / name, ENGINE / "bin" / f"{name}.cmd"):
            if not shim.exists():
                continue
            body = shim.read_text(encoding="utf-8")
            offenders = [ln for ln in body.splitlines()
                         if "-m singularity" in ln and not ln.lstrip().startswith(("#", "rem "))]
            assert not offenders, f"{shim.name} still launches via -m: {offenders}"


def test_cmd_shims_carry_no_absolute_engine_path():
    """A hardcoded engine path only ever works on the machine that wrote it
    (issue #15) — `bin/mb.cmd` shipped with `D:\\Singularity` in it."""
    for name in SHIMS:
        cmd = ENGINE / "bin" / f"{name}.cmd"
        if not cmd.exists():
            continue
        body = cmd.read_text(encoding="utf-8")
        for line in body.splitlines():
            if line.lstrip().startswith("rem "):
                continue
            assert ":\\" not in line and ":/" not in line, \
                f"{cmd.name} hardcodes an absolute path: {line!r}"


@pytest.mark.parametrize("tool,target", sorted(SHIMS.items()))
def test_launcher_runs_from_a_shadowing_cwd(tool, target, shadowing_cwd):
    """The fallback launcher itself — run with this interpreter, so the test
    does not depend on the engine having a built .venv."""
    proc = subprocess.run([sys.executable, str(LAUNCH), tool, target, "--version"],
                          cwd=str(shadowing_cwd), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, f"{tool}: exit {proc.returncode}, stderr: {proc.stderr[:400]}"
    assert proc.stdout.strip(), f"{tool}: no output, stderr: {proc.stderr[:400]}"
    assert "unknown location" not in proc.stderr


def test_launcher_sets_argv0_to_the_tool_name(shadowing_cwd):
    """argparse prints sys.argv[0] as the program name; `_launch.py` would be
    a confusing thing for a user to read in a usage line."""
    proc = subprocess.run([sys.executable, str(LAUNCH), "mb", "singularity.cli:main", "--help"],
                          cwd=str(shadowing_cwd), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0
    assert proc.stdout.lstrip().startswith("usage: mb"), proc.stdout[:200]


def test_launcher_rejects_a_malformed_target():
    proc = subprocess.run([sys.executable, str(LAUNCH), "mb", "singularity.cli"],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 2
    assert "usage:" in proc.stderr


@pytest.mark.skipif(shutil.which("sh") is None and shutil.which("bash") is None,
                    reason="no POSIX shell on PATH")
@pytest.mark.skipif(not (ENGINE / ".venv").exists(),
                    reason="no built .venv to launch (source checkout only)")
def test_posix_shim_runs_from_a_shadowing_cwd(shadowing_cwd):
    """End-to-end through the shim a user actually types."""
    sh = shutil.which("sh") or shutil.which("bash")
    proc = subprocess.run([sh, str(ENGINE / "bin" / "mb"), "--version"],
                          cwd=str(shadowing_cwd), capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, f"exit {proc.returncode}, stderr: {proc.stderr[:400]}"
    assert proc.stdout.strip().startswith("mb "), proc.stdout
