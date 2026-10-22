"""CLI and interruption safety of `tools/plant.py`.

Born from an incident: `plant.py tests/plants.json --help` ignored the flag and
planted every case into real repo files; the run was killed and left
`if override and True:` in `jev/client.py` (a key-check bypass). Everything
here runs on throwaway files under `tmp_path` with PLANT_ROOT pointing there --
never on the repository or `tests/plants.json`.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PLANT = Path(__file__).resolve().parents[1] / "tools" / "plant.py"
ORIGINAL = b"ANCHOR\r\n\x00\xff tail\n"          # CRLF + NUL + non-UTF-8: byte-exactness


@pytest.fixture
def world(tmp_path):
    subject = tmp_path / "subject.txt"
    subject.write_bytes(ORIGINAL)
    return tmp_path, subject


def spec_file(tmp_path, cases):
    spec = tmp_path / "cases.json"
    spec.write_text(json.dumps(cases), encoding="utf-8")
    return spec


def case(label="c", cmd=None, **kw):
    return {"file": "subject.txt", "old": "ANCHOR", "new": "PLANTED", "label": label,
            "cmd": cmd or [sys.executable, "-c", "pass"], **kw}


def sleeper(tmp_path):
    """Green on baseline; once planted, announces itself and sleeps."""
    marker = tmp_path / "planted.flag"
    code = (f"import os,pathlib,time; s=pathlib.Path(r'{tmp_path / 'subject.txt'}').read_bytes()\n"
            "if b'PLANTED' in s:\n"
            f"    pathlib.Path(r'{marker}').write_text(str(os.getpid())); time.sleep(20)\n")
    return [sys.executable, "-c", code], marker


def start(tmp_path, *args, stdin=None):
    return subprocess.Popen([sys.executable, str(PLANT), *map(str, args)],
                            cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "PLANT_ROOT": str(tmp_path)})


def run(tmp_path, *args):
    p = start(tmp_path, *args)
    out, err = p.communicate(timeout=60)
    return p.returncode, out, err


def wait_for(path, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def journals(tmp_path):
    return list((tmp_path / ".mb" / "plant").glob("*.json"))


# --- argument parsing -----------------------------------------------------------

def test_help_prints_usage_and_touches_nothing(world):
    tmp, subject = world
    spec = spec_file(tmp, [case(cmd=[sys.executable, "-c", "open('ran.flag','w')"])])
    for flag in ("--help", "-h"):
        code, out, _ = run(tmp, spec, flag)
        assert code == 0 and "--only" in out
    assert subject.read_bytes() == ORIGINAL
    assert not (tmp / "ran.flag").exists() and not (tmp / ".mb").exists()


@pytest.mark.parametrize("flag", ["--bogus", "-x", "--verbose"])
def test_unknown_flag_exits_2_and_runs_nothing(world, flag):
    tmp, subject = world
    spec = spec_file(tmp, [case(cmd=[sys.executable, "-c", "open('ran.flag','w')"])])
    code, out, err = run(tmp, spec, flag)
    assert code == 2 and "unrecognized" in err
    assert not (tmp / "ran.flag").exists() and not (tmp / ".mb").exists()
    assert subject.read_bytes() == ORIGINAL


def test_only_selects_by_label_substring_and_is_repeatable(world):
    tmp, _ = world
    spec = spec_file(tmp, [case(n, expect="GREEN") for n in ("alpha rule", "beta rule", "gamma rule")])
    code, out, _ = run(tmp, spec, "--only", "ALPHA", "-k", "gamma")
    assert "alpha rule" in out and "gamma rule" in out and "beta rule" not in out
    assert "2/2" in out


def test_only_matching_nothing_is_an_error_not_a_vacuous_pass(world):
    tmp, subject = world
    code, out, err = run(tmp, spec_file(tmp, [case("alpha")]), "--only", "nope")
    assert code == 2 and "matched no case" in err and out == ""
    assert subject.read_bytes() == ORIGINAL


# --- interruption ---------------------------------------------------------------

@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_a_signal_mid_run_restores_the_file_byte_for_byte(world, sig):
    tmp, subject = world
    cmd, marker = sleeper(tmp)
    p = start(tmp, spec_file(tmp, [case(cmd=cmd)]))
    wait_for(marker)
    assert b"PLANTED" in subject.read_bytes()          # really planted at this moment
    p.send_signal(sig)
    p.communicate(timeout=30)
    assert p.returncode != 0
    assert subject.read_bytes() == ORIGINAL
    assert journals(tmp) == []


def test_a_sigkilled_run_is_repaired_at_the_next_start(world):
    tmp, subject = world
    cmd, marker = sleeper(tmp)
    p = start(tmp, spec_file(tmp, [case(cmd=cmd)]))
    wait_for(marker)
    p.kill()
    p.communicate()
    os.kill(int(marker.read_text()), signal.SIGKILL)   # the orphaned sleeper
    assert b"PLANTED" in subject.read_bytes(), "SIGKILL must leave the plant behind (premise)"
    assert len(journals(tmp)) == 1

    code, out, err = run(tmp, spec_file(tmp, [case("later")]))
    assert subject.read_bytes() == ORIGINAL
    assert "RECOVERED" in err and str(subject) in err
    assert journals(tmp) == []
    assert "later" in out                              # the new run itself proceeded


def test_a_journal_of_a_live_process_is_left_alone(world):
    tmp, subject = world
    cmd, marker = sleeper(tmp)
    p = start(tmp, spec_file(tmp, [case(cmd=cmd)]))
    try:
        wait_for(marker)
        planted = subject.read_bytes()
        code, out, err = run(tmp, spec_file(tmp, [case("other", old="ANCHOR2", new="X")]), "-k", "other")
        assert "RECOVERED" not in err
        assert subject.read_bytes() == planted        # the live run is not sabotaged
    finally:
        p.terminate()
        p.communicate(timeout=30)
    assert subject.read_bytes() == ORIGINAL


def test_a_normal_run_leaves_no_journal_and_the_original_bytes(world):
    tmp, subject = world
    code, out, _ = run(tmp, spec_file(tmp, [case(cmd=[sys.executable, "-c", "pass"], expect="GREEN")]))
    assert code == 0
    assert subject.read_bytes() == ORIGINAL and journals(tmp) == []
