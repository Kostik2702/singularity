"""Snap the v0.5.1 daemon-cleanup fix baseline: the ownership matrix of
SearchDaemon._cleanup_state() exercised in every state a real daemon can find
its state files in, plus a live-daemon check against the running instance.

Run from the repo root of a checkout that HAS the fix:
    PYTHONIOENCODING=utf-8 python tools/daemon_cleanup_baseline.py \n        > docs/v0.5.1-daemon-cleanup-baseline.json

PYTHONIOENCODING matters on Windows: a bare redirect encodes stdout in the
console codepage and mangles every non-ASCII character in the snapshot.
"""
from __future__ import annotations

import json
import platform
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


# The checkout this script lives in must win over any editable install that
# points somewhere else — a baseline snapped against a different working tree
# is worthless. sys.path[0] is this file's directory, so prepend the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import singularity  # noqa: E402
from singularity.config import Config, load_config  # noqa: E402
from singularity.platform import proc  # noqa: E402
from singularity.search import client  # noqa: E402
from singularity.search.daemon import SearchDaemon  # noqa: E402

OWN_PID = 111
OWN_IID = "own-instance"


def _fresh_cfg(tmp: Path, name: str) -> Config:
    d = tmp / name
    d.mkdir(parents=True, exist_ok=True)
    return Config(vault=d, data={})


def _files(cfg: Config) -> dict[str, bool]:
    return {
        "daemon.json": cfg.daemon_state_file.exists(),
        "daemon.port": cfg.daemon_port_file.exists(),
        "daemon.pid": cfg.daemon_pid_file.exists(),
    }


def _seed(cfg: Config, state: dict | str | None, port="5001", pid="111") -> None:
    cfg.daemon_state_file.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(state, dict):
        proc.write_json_atomic(cfg.daemon_state_file, state)
    elif isinstance(state, str):
        cfg.daemon_state_file.write_text(state, encoding="utf-8")
    if port is not None:
        cfg.daemon_port_file.write_text(port, encoding="utf-8")
    if pid is not None:
        cfg.daemon_pid_file.write_text(pid, encoding="utf-8")


SCENARIOS = [
    ("successor_owns__different_pid",
     {"instance_id": "successor", "pid": 222, "port": 5001},
     "preserved",
     "the successor bound a port and wrote its identity while this instance was "
     "still finishing a request"),
    ("successor_owns__reused_pid",
     {"instance_id": "successor", "pid": OWN_PID, "port": 5001},
     "preserved",
     "the OS handed the successor the same pid — instance_id is what decides"),
    ("self_owns",
     {"instance_id": OWN_IID, "pid": OWN_PID, "port": 5001},
     "removed",
     "ordinary shutdown: the state still records this instance"),
    ("legacy_state_no_instance_id__self",
     {"pid": OWN_PID, "port": 5001},
     "removed",
     "pre-AUD-11 state file carries no instance_id — the pid is the fallback"),
    ("legacy_state_no_instance_id__successor",
     {"pid": 222, "port": 5001},
     "preserved",
     "same fallback, but the pid says somebody else owns it"),
    ("state_absent",
     None,
     "removed",
     "nobody claims ownership — the legacy port/pid files are this instance's "
     "own leftovers"),
    ("state_corrupt",
     "{not json",
     "removed",
     "unreadable state is treated as unclaimed, same as absent"),
]


def run_matrix(tmp: Path) -> list[dict]:
    out = []
    for name, state, expected, why in SCENARIOS:
        cfg = _fresh_cfg(tmp, name)
        _seed(cfg, state)
        before = _files(cfg)
        SearchDaemon._cleanup_state(cfg, own_pid=OWN_PID, own_instance_id=OWN_IID)
        after = _files(cfg)
        actual = "preserved" if any(after.values()) else "removed"
        out.append({
            "scenario": name,
            "why": why,
            "state_file": state if not isinstance(state, str) else "<corrupt>",
            "own": {"pid": OWN_PID, "instance_id": OWN_IID},
            "before": before,
            "after": after,
            "expected": expected,
            "actual": actual,
            "pass": actual == expected,
        })
    return out


def run_legacy_signature(tmp: Path) -> dict:
    """The reporter's own call shape — _cleanup_state(cfg, own_pid=...) with no
    instance_id — must keep working."""
    cfg = _fresh_cfg(tmp, "reporter_signature")
    _seed(cfg, {"instance_id": "new", "pid": 222})
    SearchDaemon._cleanup_state(cfg, own_pid=OWN_PID)
    return {"call": "_cleanup_state(cfg, own_pid=111)",
            "after": _files(cfg),
            "expected": "preserved",
            "pass": cfg.daemon_state_file.exists()}


def run_live_check() -> dict:
    """Real machine: play the exiting predecessor against the LIVE daemon."""
    try:
        cfg = load_config()
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "reason": f"no vault: {exc}"}
    before = _files(cfg)
    if not before["daemon.json"]:
        return {"ran": False, "reason": "no daemon state present", "before": before}
    ping_before = client.ping(cfg)
    SearchDaemon._cleanup_state(cfg, own_pid=999999,
                                own_instance_id="predecessor-that-just-exited")
    after = _files(cfg)
    ping_after = client.ping(cfg)
    return {
        "ran": True,
        "vault": str(cfg.vault),
        "live_instance_id": (ping_before or {}).get("instance_id"),
        "live_pid": (ping_before or {}).get("pid"),
        "before": before,
        "after": after,
        "ping_before_ok": bool(ping_before),
        "ping_after_ok": bool(ping_after),
        "pass": all(after.values()) and bool(ping_after),
    }


def run_pytest(target: str) -> dict:
    r = subprocess.run([sys.executable, "-m", "pytest", target, "-q",
                        "--ignore=tests/test_gemini_e2e.py"],
                       capture_output=True, text=True)
    tail = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {"target": target, "exit_code": r.returncode,
            "summary": tail[-1].strip() if tail else "<no summary>"}


def main() -> None:
    # A baseline that silently exercised a DIFFERENT checkout is worse than no
    # baseline: an editable install resolves `singularity` to wherever it was
    # installed from, not to the cwd.
    pkg = Path(singularity.__file__).resolve().parent
    if pkg.parent != Path.cwd().resolve():
        raise SystemExit(f"refusing to snap: imported {pkg}, cwd is {Path.cwd()}")

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        matrix = run_matrix(tmp)
        legacy = run_legacy_signature(tmp)
    snap = {
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "note": "v0.5.1 fix baseline: SearchDaemon._cleanup_state() ownership "
                "matrix (issue #8)",
        "version": singularity.__version__,
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                 text=True).stdout.strip(),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "package_path": str(Path(singularity.__file__).resolve()),
        },
        "subject": "singularity/search/daemon.py :: SearchDaemon._cleanup_state",
        "ownership_matrix": matrix,
        "reporter_signature_compat": legacy,
        "live_daemon_check": run_live_check(),
        "tests": [run_pytest("tests/test_daemon_identity.py"), run_pytest("tests/")],
    }
    snap["all_pass"] = (
        all(s["pass"] for s in matrix)
        and legacy["pass"]
        and snap["live_daemon_check"].get("pass", None) is not False
        and all(t["exit_code"] == 0 for t in snap["tests"])
    )
    print(json.dumps(snap, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
