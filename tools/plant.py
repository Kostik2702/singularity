"""Plant a violation, watch the check go red, restore. Reusable.

§2 already says a new invariant is proven by planting the violation it must
catch. The rule was never the problem — the cost was. Hand-rolling
save/mutate/run/restore takes minutes each time, so under pressure it gets
skipped, and a suite then reports green over checks that cannot fail.

Usage — a JSON list of cases on stdin or in a file:

    [{"file": "singularity/machine/routing.py",
      "old": "MIN_PROMPT_CHARS = 10", "new": "MIN_PROMPT_CHARS = 14",
      "cmd": ["pytest", "-q", "tests/test_routing.py"], "expect": "RED",
      "label": "threshold silences short tasks"}]

    python tools/plant.py cases.json

Exit code is 0 only when every case matched its expectation, so this can be a
gate rather than a report someone reads.

Four properties it will not trade. Each was a defect in the first version,
found by planting violations against this file:

- **Three verdicts, not two.** A non-zero exit means the command failed, not
  that the gate caught anything. `pytest` exits 1 for a failed test and 4 for a
  usage or collection error; a missing binary is 127. Collapsing them certified
  gates that never ran — a typo'd flag reported as a catch.
- **The baseline runs first.** A case whose target suite was already red passes
  for free. The check is: green before the plant, red after. Anything else is
  ERROR, never a catch.
- **The restore is verified, and so is the write that performs it.** Bytes live
  in memory and are compared after writing back. A write that never happens —
  read-only file, full disk — used to escape as a bare OSError with the planted
  defect left on disk. These files are untracked by design, so there is no
  `git checkout` to fall back on: the loud stop IS the recovery path.
- **No case may reach outside the root.** The tool's entire job is overwriting
  source files.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("PLANT_ROOT", Path(__file__).resolve().parents[1]))


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run(cmd, cwd, timeout) -> tuple[int | None, str]:
    """(exit code, last meaningful line). None means the command never ran."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return None, f"timeout after {timeout}s"
    tail = [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()][-1:]
    return proc.returncode, tail[0][:70] if tail else ""


def run_case(case: dict) -> tuple[str, str]:
    """(verdict, detail). RED / GREEN / ERROR / SKIP.

    RED and GREEN are claims about the CHECK. ERROR says the run proved nothing
    and must never be read as either."""
    path = (ROOT / case["file"]).resolve()
    if ROOT.resolve() not in path.parents and path != ROOT.resolve():
        return "SKIP", f"{case['file']} is outside {ROOT}"
    if not path.is_file():
        return "SKIP", f"no such file: {case['file']}"

    old, new = case["old"].encode(), case["new"].encode()
    if old == new:
        return "SKIP", "old == new: the run would measure an unchanged file"

    original = path.read_bytes()
    digest = _digest(original)
    hits = original.count(old)
    if hits != 1:
        return "SKIP", f"anchor appears {hits}× in {case['file']}"

    cwd = ROOT / case.get("cwd", ".")
    timeout = case.get("timeout", 900)

    # Baseline first: a check that is already failing cannot demonstrate a catch.
    base_code, base_detail = _run(case["cmd"], cwd, timeout)
    if base_code is None:
        return "ERROR", base_detail
    if base_code != 0:
        return "ERROR", f"baseline is not green (exit={base_code}) {base_detail}"

    try:
        path.write_bytes(original.replace(old, new))
        code, detail = _run(case["cmd"], cwd, timeout)
    finally:
        try:
            path.write_bytes(original)
            restored = _digest(path.read_bytes())
        except OSError as exc:
            raise SystemExit(f"RESTORE FAILED for {path}: {exc} — the planted "
                             f"defect is still on disk, stop and inspect") from exc
        if restored != digest:
            raise SystemExit(f"RESTORE FAILED for {path} — bytes differ, stop and inspect")

    if code is None:
        return "ERROR", detail
    if code == 1:
        return "RED", f"exit=1 {detail}"
    if code == 0:
        return "GREEN", f"exit=0 {detail}"
    return "ERROR", f"exit={code} — command broke, the gate never ran; {detail}"


def main(argv: list[str]) -> int:
    raw = Path(argv[1]).read_text(encoding="utf-8") if len(argv) > 1 else sys.stdin.read()
    cases = json.loads(raw)
    bad = 0
    for case in cases:
        expect = case.get("expect", "RED")
        verdict, detail = run_case(case)
        ok = verdict == expect
        bad += not ok
        print(f"{'ok  ' if ok else 'MISS'} [{verdict:5}] expected {expect:5} "
              f"{case.get('label', case['file'])}  ({detail})")
    print(f"\n{len(cases) - bad}/{len(cases)} cases behaved as expected")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
