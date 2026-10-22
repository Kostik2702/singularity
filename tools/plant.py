"""Plant a violation, watch the check go red, restore. Reusable.

§2 already says a new invariant is proven by planting the violation it must
catch. The rule was never the problem — the cost was. Hand-rolling
save/mutate/run/restore takes minutes each time, so under pressure it gets
skipped, and a suite then reports green over checks that cannot fail.

Usage — a JSON list of cases on stdin or in a file (`-h` prints the help and
runs nothing; an unknown flag exits 2 and runs nothing):

    [{"file": "singularity/machine/routing.py",
      "old": "MIN_PROMPT_CHARS = 10", "new": "MIN_PROMPT_CHARS = 14",
      "cmd": ["pytest", "-q", "tests/test_routing.py"], "expect": "RED",
      "label": "threshold silences short tasks"}]

    python tools/plant.py cases.json
    python tools/plant.py cases.json --only "short tasks" -k routing

`--only SUBSTR` / `-k SUBSTR` (repeatable) keeps the cases whose label — or
file, when unlabelled — contains the substring, case-insensitively. Run ONE
plant while working on one rule; never launch the whole file to "see what
happens". A filter that matches nothing is exit 2, not a vacuous 0/0 pass.

A command word `{python}` is the interpreter running this tool. A case
declared with a path to one machine's venv (`.venv/bin/python`) found no
interpreter on Windows, where a venv keeps it in `Scripts`, nor on a CI runner,
which has no venv at all -- every such case came back ERROR everywhere but the
machine that wrote it.

Exit code is 0 only when every case matched its expectation, so this can be a
gate rather than a report someone reads.

Six properties it will not trade. Each was a defect found by planting
violations against this file — the fifth while proving a token-accounting
invariant on 2026-08-07, the sixth after a killed run left `jev/client.py`
with a key-check bypass on disk:

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
- **The bytecode cache is dropped around every write.** A plant that keeps the
  file's size — `0.1` -> `1.0` — inside the same second as the last compile
  matches the `.pyc` header on both (mtime, size), so the stale bytecode runs
  and the tool reports GREEN about an invariant that does catch its violation.
  See `_drop_bytecode`.
- **An interrupted run puts the file back.** Before a write the original bytes
  go to a journal in `<root>/.mb/plant/`; the restore runs in `finally`, and
  SIGINT/SIGTERM/SIGHUP are turned into a normal exit so `finally` runs. SIGKILL
  cannot be caught, so the next start of the tool restores every journal whose
  owning process is dead, byte for byte, and says so on stderr.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(os.environ.get("PLANT_ROOT", Path(__file__).resolve().parents[1]))
PYTHON = "{python}"                     # a command word: the interpreter running this tool


_restoring = False                      # a signal during the restore must not cut it short


def _journal_dir() -> Path:
    return ROOT / ".mb" / "plant"


def _journal_path(path: Path) -> Path:
    return _journal_dir() / (hashlib.sha256(str(path).encode()).hexdigest()[:16] + ".json")


def _journal_write(path: Path, original: bytes) -> Path:
    """Persist the original bytes BEFORE the file is touched (atomic rename)."""
    journal = _journal_path(path)
    journal.parent.mkdir(parents=True, exist_ok=True)
    entry = {"file": str(path), "pid": os.getpid(),
             "sha256": _digest(original), "bytes": base64.b64encode(original).decode()}
    tmp = journal.with_suffix(".tmp")
    tmp.write_text(json.dumps(entry), encoding="utf-8")
    os.replace(tmp, journal)
    return journal


def _pid_alive(pid: int) -> bool:
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                     # exists, not ours
        return True
    return True


def recover_stale() -> list[str]:
    """Restore files left planted by a previous run that died without cleanup
    (SIGKILL, power loss). Journals of a still-running process are left alone."""
    messages = []
    for journal in sorted(_journal_dir().glob("*.json")):
        try:
            entry = json.loads(journal.read_text(encoding="utf-8"))
            original = base64.b64decode(entry["bytes"])
            path = Path(entry["file"])
            pid = int(entry["pid"])
        except (OSError, ValueError, KeyError):
            messages.append(f"UNREADABLE plant journal {journal} — inspect by hand")
            continue
        if _pid_alive(pid):
            continue
        if not path.exists() or path.read_bytes() != original:
            path.write_bytes(original)
            _drop_bytecode(path)
            messages.append(f"RECOVERED {path} from an interrupted plant run (pid {pid})")
        if path.read_bytes() != original:
            raise SystemExit(f"RESTORE FAILED for {path} — bytes differ, stop and inspect")
        journal.unlink(missing_ok=True)
    return messages


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _drop_bytecode(path: Path) -> None:
    """Delete every cached compile of `path`, so the next run reads the bytes
    just written.

    CPython invalidates a `.pyc` by comparing the source's (mtime, size) with
    the pair recorded in the cache header, and mtime is stored with SECOND
    granularity. A plant that does not change the file's size — `0.1` -> `1.0`,
    `True` -> `False`, one digit for another — run inside the same second as
    the previous compile therefore matches on both, and the interpreter loads
    the STALE bytecode: the planted defect never runs.

    That failure is silent and points the wrong way. The tool reports GREEN,
    i.e. "this invariant does not catch its violation", about an invariant that
    catches it perfectly — so the reader goes off to rewrite a healthy test.
    A gate whose whole purpose is finding checks that cannot fail must not have
    a case where it cannot fail itself.

    Both the standard cache path (which honours PYTHONPYCACHEPREFIX) and every
    sibling `__pycache__/<stem>.*.pyc` are removed: pytest's assertion-rewritten
    copies carry their own tag, so `cache_from_source` alone would miss them.
    """
    if path.suffix != ".py":
        return
    try:
        cached = Path(importlib.util.cache_from_source(str(path)))
    except (ValueError, NotImplementedError):        # pragma: no cover
        cached = None
    if cached is not None:
        cached.unlink(missing_ok=True)
    for stale in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
        stale.unlink(missing_ok=True)


def _run(cmd, cwd, timeout) -> tuple[int | None, str]:
    """(exit code, last meaningful line). None means the command never ran."""
    cmd = [sys.executable if word == PYTHON else word for word in cmd]
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

    journal = _journal_write(path, original)
    global _restoring
    try:
        path.write_bytes(original.replace(old, new))
        _drop_bytecode(path)
        code, detail = _run(case["cmd"], cwd, timeout)
    finally:
        _restoring = True
        try:
            path.write_bytes(original)
            # The restore needs this as much as the plant does: a cache built
            # FROM the planted bytes would otherwise survive into the next run
            # and quietly test the defect instead of the restored file.
            _drop_bytecode(path)
            restored = _digest(path.read_bytes())
        except OSError as exc:
            raise SystemExit(f"RESTORE FAILED for {path}: {exc} — the planted "
                             f"defect is still on disk, stop and inspect") from exc
        finally:
            _restoring = False
        if restored != digest:
            raise SystemExit(f"RESTORE FAILED for {path} — bytes differ, stop and inspect")
        journal.unlink(missing_ok=True)

    if code is None:
        return "ERROR", detail
    if code == 1:
        return "RED", f"exit=1 {detail}"
    if code == 0:
        return "GREEN", f"exit=0 {detail}"
    return "ERROR", f"exit={code} — command broke, the gate never ran; {detail}"


def _on_signal(signum, _frame):
    """Turn a termination signal into a normal exit so `finally` restores the
    file. Ignored while the restore itself is running."""
    if not _restoring:
        raise SystemExit(128 + signum)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="plant.py",
        description="Plant a violation, expect the check to go RED, restore the file. "
                    "Reads a JSON list of cases from SPEC or stdin.")
    ap.add_argument("spec", nargs="?", help="JSON file with the cases (default: stdin)")
    ap.add_argument("--only", "-k", action="append", default=[], metavar="SUBSTR",
                    help="run only cases whose label (or file) contains SUBSTR, "
                         "case-insensitive; repeatable")
    return ap


def main(argv: list[str]) -> int:
    ap = _parser()
    args = ap.parse_args(argv[1:])          # -h exits 0, an unknown flag exits 2 — before anything runs
    try:
        raw = Path(args.spec).read_text(encoding="utf-8") if args.spec else sys.stdin.read()
        cases = json.loads(raw)
    except (OSError, ValueError) as exc:
        ap.error(f"cannot read cases: {exc}")
    if args.only:
        needles = [n.lower() for n in args.only]
        cases = [c for c in cases
                 if any(n in c.get("label", c["file"]).lower() for n in needles)]
        if not cases:
            ap.error(f"--only {args.only} matched no case")
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
        signal.signal(sig, _on_signal)
    for msg in recover_stale():
        print(msg, file=sys.stderr)
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
