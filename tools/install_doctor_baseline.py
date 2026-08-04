"""Snap the v0.6.0 install/doctor baseline (field issues #9-#16).

Unlike the v0.5.1 baseline, which pinned the contract of ONE function, this
one pins the contract of the surfaces those eight issues were about: what
`mb doctor` reports on a real machine, whether the shims survive a shadowing
CWD, where a CLI hidden by a version manager is found, which config paths are
enumerated, and whether the running daemon actually picks up a config change.

Every section answers a question the reporter had to answer by hand.

Run from the repo root of a checkout that HAS the fixes:

    PYTHONIOENCODING=utf-8 python tools/install_doctor_baseline.py \
        > docs/v0.6.0-install-doctor-baseline.json

PYTHONIOENCODING matters on Windows: a bare redirect encodes stdout in the
console codepage and mangles every non-ASCII character in the snapshot. In
PowerShell, `>` additionally prepends a UTF-8 BOM that `json.load` rejects —
capture the output and write it with `[IO.File]::WriteAllText(..., UTF8Encoding
$false)` instead.
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# The checkout this script lives in must win over any editable install that
# points somewhere else — a baseline snapped against a different working tree
# is worthless (the v0.5.1 baseline learned this the hard way).
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import singularity  # noqa: E402
from singularity import config_paths, doctor  # noqa: E402
from singularity.config import load_config  # noqa: E402
from singularity.search import client  # noqa: E402

SHIMS = ("mb", "imgqa", "design-scout")


def _load_install(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- #16: the shims must survive a CWD that shadows the package --------------

def shim_matrix(tmp: Path) -> dict:
    """Run every launcher from a neutral CWD and from one holding a
    `singularity/` directory — the parent of a standard clone."""
    neutral = tmp / "neutral"
    decoy = tmp / "decoy"
    neutral.mkdir()
    (decoy / "singularity").mkdir(parents=True)
    launch = REPO / "bin" / "_launch.py"
    targets = {"mb": "singularity.cli:main",
               "imgqa": "singularity.imgqa.cli:main",
               "design-scout": "singularity.scout.cli:main"}

    rows = []
    for tool, target in targets.items():
        for label, cwd in (("neutral", neutral), ("shadowing", decoy)):
            r = subprocess.run([sys.executable, str(launch), tool, target, "--version"],
                               cwd=str(cwd), capture_output=True, text=True, timeout=180)
            rows.append({"launcher": "bin/_launch.py", "tool": tool, "cwd": label,
                         "exit_code": r.returncode,
                         "stdout": (r.stdout or "").strip()[:80],
                         "namespace_shadowed": "unknown location" in (r.stderr or ""),
                         "pass": r.returncode == 0 and bool((r.stdout or "").strip())})

    sh = shutil.which("sh") or shutil.which("bash")
    for label, cwd in (("neutral", neutral), ("shadowing", decoy)):
        if sh is None:
            rows.append({"launcher": "sh bin/mb", "cwd": label, "skipped": "no POSIX shell"})
            continue
        r = subprocess.run([sh, str(REPO / "bin" / "mb"), "--version"],
                           cwd=str(cwd), capture_output=True, text=True, timeout=180)
        rows.append({"launcher": "sh bin/mb", "tool": "mb", "cwd": label,
                     "exit_code": r.returncode,
                     "stdout": (r.stdout or "").strip()[:80],
                     "namespace_shadowed": "unknown location" in (r.stderr or ""),
                     "pass": r.returncode == 0 and bool((r.stdout or "").strip())})

    index_modes = {}
    for rel in ("install/install.sh", *[f"bin/{t}" for t in SHIMS]):
        out = subprocess.run(["git", "ls-files", "-s", "--", rel],
                             cwd=str(REPO), capture_output=True, text=True)
        index_modes[rel] = (out.stdout.split(" ", 1)[0] if out.stdout.strip() else None)

    return {
        "runs": rows,
        "executable_bit_in_index": index_modes,
        "pass": (all(r.get("pass", True) for r in rows)
                 and all(m == "100755" for m in index_modes.values())),
    }


# --- #10: a CLI hidden by a version manager ----------------------------------

def locator_matrix(tmp: Path) -> dict:
    """Synthetic version-manager layouts — real ones cannot be assumed on any
    given machine, and the point is the search order, not this host."""
    locator = _load_install("cli_locator")
    exe_name = "tool.cmd" if os.name == "nt" else "tool"
    rows = []

    layouts = {
        "nvm_versions_tree": Path(".nvm/versions/node/v24.19.0/bin"),
        "nvm_versions_tree_older": Path(".nvm/versions/node/v18.20.0/bin"),
        "volta": Path(".volta/bin"),
        "fnm": Path(".local/share/fnm/node-versions/v22.1.0/installation/bin"),
    }
    for name, rel in layouts.items():
        home = tmp / f"home-{name}"
        d = home / rel
        d.mkdir(parents=True)
        (d / exe_name).write_text("#!/bin/sh\n", encoding="utf-8")
        (d / exe_name).chmod(0o755)
        real_home = Path.home
        try:
            Path.home = classmethod(lambda cls, h=home: h)   # type: ignore[assignment]
            found, where = locator.find_cli("tool")
        finally:
            Path.home = real_home                            # type: ignore[assignment]
        rows.append({"layout": name, "found": bool(found),
                     "where_kind": "version-manager" if where and where != "PATH" else where,
                     "pass": bool(found)})

    # newest Node must win, and "newest" must be numeric, not lexicographic
    home = tmp / "home-order"
    for v in ("v9.0.0", "v10.0.0", "v24.19.0"):
        d = home / ".nvm" / "versions" / "node" / v / "bin"
        d.mkdir(parents=True)
        (d / exe_name).write_text("#!/bin/sh\n", encoding="utf-8")
    real_home = Path.home
    try:
        Path.home = classmethod(lambda cls, h=home: h)       # type: ignore[assignment]
        found, _where = locator.find_cli("tool")
    finally:
        Path.home = real_home                                # type: ignore[assignment]
    rows.append({"layout": "newest_node_wins", "found": bool(found),
                 "selected": Path(found).parent.parent.name if found else None,
                 "expected": "v24.19.0",
                 "pass": bool(found) and Path(found).parent.parent.name == "v24.19.0"})

    return {"search_locations": len(locator.search_locations()),
            "runs": rows,
            "pass": all(r["pass"] for r in rows)}


# --- #14 / #13 / #11 / #15: what doctor says about THIS machine --------------

def doctor_report(cfg) -> dict:
    """Every check id and status against the real vault. Details are kept for
    the checks whose whole point is what they say.

    `as_json=True` makes doctor print its own document to stdout, which would
    land in the middle of this snapshot — so it is swallowed and the result
    read back from `.mb/doctor-last.json`, which doctor always writes."""
    import contextlib
    import io

    with contextlib.redirect_stdout(io.StringIO()):
        rc = doctor.run(cfg, as_json=True)
    payload = json.loads((cfg.mb_dir / "doctor-last.json").read_text(encoding="utf-8"))
    verbose = {"A6-config-paths", "D7-cli-on-path", "D8-git-identity",
               "K1-code-index", "D6-posix-shim"}
    return {
        "exit_code": rc,
        "counts": payload["counts"],
        "checks": [{"id": c["id"], "status": c["status"],
                    **({"detail": c["detail"]} if c["id"] in verbose else {})}
                   for c in payload["checks"]],
        "new_in_this_release": sorted(
            c["id"] for c in payload["checks"]
            if c["id"] in {"A6-config-paths", "D7-cli-on-path", "D8-git-identity"}),
        "pass": rc == 0,
    }


def config_path_inventory(cfg) -> dict:
    """#14: every path-valued key, and whether it belongs to this OS. Keys
    only — values are this machine's business."""
    rows = []
    for key, value in config_paths.path_entries(cfg.data):
        rows.append({"key": key,
                     "foreign_os": config_paths.foreign_os(value),
                     "exists": Path(value).exists()})
    return {"entries": rows,
            "count": len(rows),
            "foreign": sorted(r["key"] for r in rows if r["foreign_os"]),
            "missing": sorted(r["key"] for r in rows
                              if not r["foreign_os"] and not r["exists"])}


# --- #12: does the LIVE daemon pick up a config change? ----------------------

LIVE_KEY = "excerpt_chars"


def live_config_reload(cfg) -> dict:
    """Set a harmless key, prove the daemon served the old value, reload,
    prove it serves the new one, then restore. Read-back + restore make this
    safe to run against a real machine."""
    answered, before = client.daemon_value(cfg, LIVE_KEY)
    if not answered:
        return {"ran": False,
                "reason": "no daemon answered the config read-back "
                          "(down, or predates v0.6.0)"}
    original = cfg.get(LIVE_KEY)
    probe = int(original) + 1
    try:
        cfg.data = {**cfg.data, LIVE_KEY: probe}
        cfg.save()
        _answered, stale = client.daemon_value(cfg, LIVE_KEY)
        reload_resp = client.reload_daemon(cfg) or {}
        _answered, fresh = client.daemon_value(cfg, LIVE_KEY)
    finally:
        cfg.data = {**cfg.data, LIVE_KEY: original}
        cfg.save()
        client.reload_daemon(cfg)
    _answered, restored = client.daemon_value(cfg, LIVE_KEY)
    return {
        "ran": True,
        "key": LIVE_KEY,
        "daemon_value_before_write": before,
        "daemon_value_after_write_before_reload": stale,
        "reload_reported_changed": reload_resp.get("config_changed"),
        "daemon_value_after_reload": fresh,
        "restored_to": restored,
        "pass": (stale == original and fresh == probe
                 and LIVE_KEY in (reload_resp.get("config_changed") or [])
                 and restored == original),
    }


def run_pytest(target: str) -> dict:
    r = subprocess.run([sys.executable, "-m", "pytest", target, "-q",
                        "--ignore=tests/test_gemini_e2e.py"],
                       cwd=str(REPO), capture_output=True, text=True)
    tail = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {"target": target, "exit_code": r.returncode,
            "summary": tail[-1].strip() if tail else "<no summary>"}


def main() -> None:
    pkg = Path(singularity.__file__).resolve().parent
    if pkg.parent != REPO:
        raise SystemExit(f"refusing to snap: imported {pkg}, repo is {REPO}")

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        shims = shim_matrix(tmp)
        locator = locator_matrix(tmp)

    snap = {
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "note": "v0.6.0 baseline: install + doctor honesty (field issues #9-#16)",
        "version": singularity.__version__,
        "commit": subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO),
                                 capture_output=True, text=True).stdout.strip(),
        "environment": {
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "package_path": str(Path(singularity.__file__).resolve()),
            "vault": str(cfg.vault),
        },
        "issue_9_16_launchers": shims,
        "issue_10_cli_locator": locator,
        "issue_11_13_14_15_doctor": doctor_report(cfg),
        "issue_14_config_paths": config_path_inventory(cfg),
        "issue_12_live_config_reload": live_config_reload(cfg),
        "tests": [run_pytest("tests/")],
    }
    snap["all_pass"] = (
        shims["pass"]
        and locator["pass"]
        and snap["issue_12_live_config_reload"].get("pass", None) is not False
        and all(t["exit_code"] == 0 for t in snap["tests"])
    )
    print(json.dumps(snap, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
