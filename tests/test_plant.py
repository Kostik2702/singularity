"""`tools/plant.py` proves other checks can fail. This proves IT can.

The first version of this file greped the tool's source for `sha256` and
`returncode`. Both words also occur in its docstring and in an f-string, so the
asserts survived deleting the restore verification and survived replacing the
exit-code verdict with a substring match on stdout — the exact defect
[[A pipe over the test command must not swallow the exit code]] exists to prevent.
That is [[Rule - a source scanner must not write what it searches for]] committed by the test
of the tool that exists to catch it.

So nothing here reads the source. Every test drives `run_case` and asserts on
what it returns or raises.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "tools"))
import plant  # noqa: E402


@pytest.fixture
def target(tmp_path, monkeypatch):
    """A file inside a throwaway ROOT, so a mutation never touches the repo."""
    monkeypatch.setattr(plant, "ROOT", tmp_path)
    f = tmp_path / "subject.txt"
    f.write_text("ANCHOR\n", encoding="utf-8")
    return f


def probe_cmd(target, code: int = 1) -> list[str]:
    """Green while the anchor stands, exits `code` once it is replaced.

    It has to be green on the baseline or `run_case` returns ERROR from the
    baseline guard and never reaches the logic under test — three tests here
    passed that way, asserting ERROR and getting it for the wrong reason."""
    return [sys.executable, "-c",
            f"import pathlib,sys; sys.exit({code} if 'PLANTED' in "
            f"pathlib.Path(r'{target}').read_text() else 0)"]


def case(**kw):
    base = {"file": "subject.txt", "old": "ANCHOR", "new": "PLANTED",
            "cmd": [sys.executable, "-c", "pass"]}
    base.update(kw)
    return base


# --- the three verdicts must be three, not two --------------------------------

def test_a_check_that_catches_the_plant_is_red(target):
    assert plant.run_case(case(cmd=probe_cmd(target)))[0] == "RED"


def test_a_passing_check_is_green(target):
    assert plant.run_case(case(cmd=[sys.executable, "-c", "pass"]))[0] == "GREEN"


@pytest.mark.parametrize("exit_code", [2, 4, 5, 127])
def test_a_command_that_broke_after_the_plant_is_not_a_catch(target, exit_code):
    """The blocker this file was written for. `pytest` exits 1 for a failed
    test and 4 for a usage/collection error; a missing binary is 127. Collapsing
    them into "non-zero means the gate caught it" certifies a gate that never
    ran — review reproduced it with a typo'd flag and a missing test file, both
    reported as `ok RED`.

    The probe is green before the plant, so the baseline guard passes and the
    exit code genuinely reaches the classification."""
    verdict, detail = plant.run_case(case(cmd=probe_cmd(target, exit_code)))
    assert verdict == "ERROR", f"exit {exit_code} was read as {verdict}: {detail}"
    assert "gate never ran" in detail, "the reason must say the run proved nothing"


def test_a_missing_binary_is_an_error_naming_the_binary(target):
    """Here the command fails at baseline too, so ERROR is right for a second
    reason — and the detail has to say which, or the two are indistinguishable."""
    verdict, detail = plant.run_case(case(cmd=["no-such-binary-anywhere"]))
    assert verdict == "ERROR" and "not found" in detail


def test_a_timeout_is_an_error_naming_the_timeout(target):
    verdict, detail = plant.run_case(case(
        cmd=[sys.executable, "-c", "import time; time.sleep(5)"], timeout=1))
    assert verdict == "ERROR" and "timeout" in detail.lower()


# --- a check that was already failing proves nothing ---------------------------

def test_a_suite_that_is_red_before_the_plant_is_an_error(target):
    """Without a baseline run, every `expect: RED` case passes for free against
    an already-broken suite — the harness would certify a whole batch of gates
    while measuring only that the tree was red to begin with."""
    verdict, detail = plant.run_case(case(
        cmd=[sys.executable, "-c", "raise SystemExit(1)"]))
    # the command fails identically with and without the mutation
    assert verdict == "ERROR", f"got {verdict}: {detail}"
    assert "baseline" in detail.lower()


def test_a_real_gate_is_still_red(target):
    """Control for the test above: if the baseline check turned everything into
    ERROR, the harness would be uniformly useless and equally green here."""
    assert plant.run_case(case(cmd=probe_cmd(target)))[0] == "RED"


# --- the restore is the property the tool cannot trade -------------------------

def test_the_file_is_restored_byte_for_byte(target):
    before = target.read_bytes()
    plant.run_case(case())
    assert target.read_bytes() == before


def test_a_restore_that_cannot_be_written_stops_loudly(target, monkeypatch):
    """Review broke this two ways. Corrupted bytes already raised; a write that
    never happens (read-only file, full disk) escaped as a bare OSError with the
    planted defect left on disk and no recovery path — these files are untracked
    by design, so `git checkout` is not available."""
    real = Path.write_bytes

    def refuse(self, data):
        if self.name == "subject.txt" and data == b"ANCHOR\n":
            raise OSError(28, "No space left on device")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", refuse)
    with pytest.raises(SystemExit, match="RESTORE FAILED"):
        plant.run_case(case(cmd=probe_cmd(target)))


# --- anchors and containment ---------------------------------------------------

def test_an_anchor_that_is_not_unique_is_skipped_not_guessed(target):
    target.write_text("ANCHOR ANCHOR\n", encoding="utf-8")
    verdict, detail = plant.run_case(case())
    assert verdict == "SKIP" and "2" in detail


def test_a_no_op_mutation_is_refused(target):
    """`old == new` runs the check against an unchanged file and reports GREEN,
    which under `expect: GREEN` is a silent pass that measured nothing."""
    assert plant.run_case(case(new="ANCHOR"))[0] == "SKIP"


def test_a_path_outside_the_root_is_refused(target):
    """The tool's whole job is writing source files; an absolute path in a case
    file would let it write anywhere."""
    verdict, _ = plant.run_case(case(file="/tmp/escape-attempt.txt"))
    assert verdict == "SKIP"


# --- the bytecode cache cannot outlive a write ---------------------------------

def _pyc_files(module: Path) -> list[Path]:
    return sorted((module.parent / "__pycache__").glob(f"{module.stem}.*.pyc"))


def test_no_compiled_cache_of_the_module_survives_a_case(tmp_path, monkeypatch):
    """CPython matches a `.pyc` to its source on (mtime, size), and mtime has
    SECOND granularity — so a plant that keeps the file's length (`0.1` -> `1.0`)
    inside the same second as the last compile runs the STALE bytecode. The
    planted defect never executes and the tool reports GREEN, i.e. "this check
    does not catch its violation", about a check that catches it perfectly.

    Reproduced on 2026-08-07 with two cases against one file: the first one's
    restore stamped a fresh mtime, and the second (`CACHE_READ = 0.1` -> `1.0`)
    came back GREEN while the same test went red by hand.

    Asserting on the cache rather than on that race keeps the test
    deterministic: if a compile from either write survives the case, the next
    case can read it.
    """
    monkeypatch.setattr(plant, "ROOT", tmp_path)
    module = tmp_path / "subject_mod.py"
    module.write_text("VALUE = 0.1\n", encoding="utf-8")
    cmd = [sys.executable, "-c",
           "import subject_mod, sys; sys.exit(0 if subject_mod.VALUE == 0.1 else 1)"]

    verdict, detail = plant.run_case(
        {"file": module.name, "old": "VALUE = 0.1", "new": "VALUE = 1.0", "cmd": cmd})

    assert verdict == "RED", detail
    assert _pyc_files(module) == [], \
        "a compile of the planted or restored file outlived the case"


def test_dropping_bytecode_also_removes_foreign_tagged_caches(tmp_path):
    """`cache_from_source` names one path; pytest's assertion-rewritten copies
    carry their own tag and sit next to it. Removing only the first would leave
    the rewritten copy to be picked up by the next run."""
    module = tmp_path / "mod.py"
    module.write_text("VALUE = 1\n", encoding="utf-8")
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    standard = Path(importlib.util.cache_from_source(str(module)))
    standard.parent.mkdir(parents=True, exist_ok=True)
    standard.write_bytes(b"stale")
    rewritten = cache / "mod.cpython-999-pytest-8.9.9.pyc"
    rewritten.write_bytes(b"stale")

    plant._drop_bytecode(module)

    assert not standard.exists() and not rewritten.exists()


def test_a_non_python_target_does_not_evict_a_neighbours_cache(tmp_path):
    """`cache_from_source("subject.txt")` does not raise — it quietly returns
    `__pycache__/subject.cpython-XXX.pyc`, which belongs to `subject.py` next
    to it. A case that plants into `config.json` would then delete the compile
    of `config.py`, a file it never touched."""
    sibling = tmp_path / "subject.py"
    sibling.write_text("VALUE = 1\n", encoding="utf-8")
    its_cache = Path(importlib.util.cache_from_source(str(sibling)))
    its_cache.parent.mkdir(parents=True, exist_ok=True)
    its_cache.write_bytes(b"compiled")

    target = tmp_path / "subject.txt"
    target.write_text("ANCHOR\n", encoding="utf-8")
    plant._drop_bytecode(target)          # must not raise, must not evict

    assert its_cache.exists(), "editing subject.txt evicted the cache of subject.py"


# --- the batch reports, and the exit code is the gate --------------------------

def test_the_batch_reports_every_case_even_when_one_errors(tmp_path, monkeypatch):
    subject = tmp_path / "subject.txt"
    subject.write_text("ANCHOR\n", encoding="utf-8")
    cases = [
        {**case(cmd=["no-such-binary-anywhere"]), "label": "broken"},
        {**case(cmd=probe_cmd(subject)), "label": "real", "expect": "RED"},
    ]
    spec = tmp_path / "cases.json"
    spec.write_text(json.dumps(cases), encoding="utf-8")
    proc = subprocess.run([sys.executable, str(ENGINE / "tools" / "plant.py"), str(spec)],
                          capture_output=True, text=True, env={**__import__("os").environ,
                                                               "PLANT_ROOT": str(tmp_path)})
    assert "broken" in proc.stdout and "real" in proc.stdout, \
        "one bad case must not swallow the rest of the report"
    assert proc.returncode != 0, "a batch containing an ERROR must not exit 0"
