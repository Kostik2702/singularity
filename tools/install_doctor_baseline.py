"""Snap the install/doctor baseline (field issues #9-#16, extended in v0.9.0).

Unlike the v0.5.1 baseline, which pinned the contract of ONE function, this
one pins the contract of the surfaces those eight issues were about: what
`mb doctor` reports on a real machine, whether the shims survive a shadowing
CWD, where a CLI hidden by a version manager is found, which config paths are
enumerated, whether the running daemon actually picks up a config change, and
— since v0.6.1 — whether the command that makes that change reaches the value
it names. The v0.6.0 baseline asked the first question and skipped the second:
it wrote through `cfg.data` on a flat key, so `mb config set` resolving no
dotted key at all was invisible to it while `A6` printed dotted keys as the
fix.

v0.9.0 adds the question that release is about: the config left the vault for
`~/.singularity.json`, and a relocation nobody asked for is only acceptable if
it is invisible — an upgrading machine keeps its values untold, and a machine
that already has a config is never overwritten by an arriving vault carrying
someone else's. That same move broke the two sections above until they were
taught it: `MB_HOME` no longer isolates a run, because it moves the vault and
leaves the command reading this machine's real config.

Every section answers a question the reporter had to answer by hand.

Run from the repo root of a checkout that HAS the fixes:

    PYTHONIOENCODING=utf-8 python tools/install_doctor_baseline.py \
        > docs/v0.11.0-install-doctor-baseline.json

PYTHONIOENCODING matters on Windows: a bare redirect encodes stdout in the
console codepage and mangles every non-ASCII character in the snapshot. In
PowerShell, `>` additionally prepends a UTF-8 BOM that `json.load` rejects —
capture the output and write it with `[IO.File]::WriteAllText(..., UTF8Encoding
$false)` instead.
"""
from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import platform
import re
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

import singularity
from singularity.config import DEFAULT_CONFIG  # noqa: E402
from singularity import doctor
from singularity.machine import config_paths# noqa: E402
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

# `cli_locator` reads these before falling back to `~`, so a synthetic layout
# under a faked `Path.home()` is invisible whenever the snapping shell has nvm
# or fnm sourced — the matrix then grades the operator's terminal instead of
# the search order. `tests/conftest.py` neutralizes the same surface for the
# suite (`_no_real_version_managers`, issue #10); the baseline had no such
# guard and scored 3/5 for exactly that reason.
_VERSION_MANAGER_ENV = ("NVM_BIN", "NVM_DIR", "FNM_MULTISHELL_PATH")


@contextlib.contextmanager
def _without_version_manager_env():
    saved = {k: os.environ.pop(k) for k in _VERSION_MANAGER_ENV if k in os.environ}
    try:
        yield sorted(saved)
    finally:
        os.environ.update(saved)


def locator_matrix(tmp: Path) -> dict:
    """Synthetic version-manager layouts — real ones cannot be assumed on any
    given machine, and the point is the search order, not this host."""
    with _without_version_manager_env() as suppressed:
        out = _locator_matrix(tmp)
    out["suppressed_env"] = suppressed
    return out


def _locator_matrix(tmp: Path) -> dict:
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
               "K1-code-index", "D6-posix-shim", "K2-code-graph"}
    return {
        "exit_code": rc,
        "counts": payload["counts"],
        "checks": [{"id": c["id"], "status": c["status"],
                    **({"detail": c["detail"]} if c["id"] in verbose else {})}
                   for c in payload["checks"]],
        # v0.19.0 adds no check: it widens what K2 counts to the jvm family
        "new_in_this_release": sorted(
            c["id"] for c in payload["checks"]
            if c["id"] in set()),
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


# --- the write path itself: does `mb config set` REACH a nested key? ---------

def _mb(args: list[str], vault: Path, home: Path) -> subprocess.CompletedProcess:
    """The real CLI, through the launcher, against a throwaway vault AND a
    throwaway home.

    Since v0.9.0 the config is a machine file at `~/.singularity.json`, so
    `MB_HOME` alone no longer isolates a run: it moves the vault and leaves the
    command reading (and, worse, writing) this machine's real config. Both
    `USERPROFILE` and `HOME` are redirected because `Path.home()` reads the
    first on Windows and the second everywhere else."""
    env = {**os.environ, "MB_HOME": str(vault), "PYTHONIOENCODING": "utf-8",
           "USERPROFILE": str(home), "HOME": str(home)}
    return subprocess.run(
        [sys.executable, str(REPO / "bin" / "_launch.py"), "mb",
         "singularity.cli:main", *args],
        cwd=str(REPO), env=env, capture_output=True, text=True,
        # The child writes UTF-8 (PYTHONIOENCODING above); without saying so
        # here the parent decodes it in the console codepage and every em-dash
        # in a captured diagnostic lands in the snapshot as mojibake.
        encoding="utf-8", errors="replace", timeout=180)


def nested_config_write(tmp: Path) -> dict:
    """Type the fix `mb doctor` prints, then check the file it claims to fix.

    The v0.6.0 baseline proved a live daemon picks up a config change — but it
    wrote through `cfg.data` directly and only ever touched a flat key, so it
    could not see that `mb config set` resolved no dotted key at all. Every key
    `A6-config-paths` names is nested; the printed fix produced a literal
    top-level key, reported success, was confirmed by `mb daemon reload` and
    `mb config validate`, and left the WARN exactly where it was.

    So this section goes through the command a user actually types, and grades
    it on the config as it lands on disk."""
    vault = tmp / "config-write"
    (vault / ".mb").mkdir(parents=True)
    # v0.9.0: the config is a MACHINE file, so the throwaway config goes in a
    # throwaway home, not in the throwaway vault. Written straight to its final
    # place — a copy in the vault would trip the one-time migration and grade
    # the migration instead of the write path.
    home = tmp / "config-write-home"
    home.mkdir()
    cfg_file = home / ".singularity.json"
    cfg_file.write_text(json.dumps({
        "vault_path": str(vault),
        "projects": {"p": {"path": r"E:\P", "group": "g", "about": "a"}},
        "llm": {"models": {"m": {"args": ["-ngl", "99",
                                          "--chat-template-file",
                                          "E:/t.jinja"]}}},
    }, indent=2), encoding="utf-8")

    def on_disk() -> dict:
        return json.loads(cfg_file.read_text(encoding="utf-8"))

    rows = []

    r = _mb(["config", "set", "projects.p.path", "/posix/p"], vault, home)
    data = on_disk()
    rows.append({"case": "dotted key reaches the nested value",
                 "cmd": "config set projects.p.path /posix/p",
                 "exit_code": r.returncode,
                 "nested_value": data["projects"]["p"]["path"],
                 "literal_top_level_keys": [k for k in data if "." in k],
                 "siblings_kept": sorted(data["projects"]["p"]) == ["about", "group", "path"],
                 "pass": (r.returncode == 0
                          and data["projects"]["p"]["path"] == "/posix/p"
                          and not [k for k in data if "." in k]
                          and data["projects"]["p"]["group"] == "g")})

    r = _mb(["config", "set", "llm.models.m.args[3]", "/posix/t.jinja"], vault, home)
    data = on_disk()
    rows.append({"case": "indexed key (the args[i] form A6 prints)",
                 "cmd": "config set llm.models.m.args[3] /posix/t.jinja",
                 "exit_code": r.returncode,
                 "value": data["llm"]["models"]["m"]["args"][3],
                 "pass": (r.returncode == 0
                          and data["llm"]["models"]["m"]["args"][3] == "/posix/t.jinja"
                          and data["llm"]["models"]["m"]["args"][2] == "--chat-template-file")})

    before = on_disk()
    r = _mb(["config", "set", "projects.ghost.path", "/x"], vault, home)
    rows.append({"case": "unknown section fails loudly, writes nothing",
                 "cmd": "config set projects.ghost.path /x",
                 "exit_code": r.returncode,
                 "stderr": (r.stderr or "").strip()[:120],
                 "pass": (r.returncode == 1 and on_disk() == before
                          and "no such config section" in (r.stderr or ""))})

    r = _mb(["config", "get", "projects.p.path"], vault, home)
    rows.append({"case": "read-back answers for the same notation",
                 "cmd": "config get projects.p.path",
                 "exit_code": r.returncode,
                 "stdout": (r.stdout or "").strip()[:80],
                 "pass": r.returncode == 0 and "/posix/p" in (r.stdout or "")})

    r = _mb(["project", "add", "p", "/posix/p2"], vault, home)
    entry = on_disk()["projects"]["p"]
    rows.append({"case": "project add repoints without dropping metadata",
                 "cmd": "project add p /posix/p2",
                 "exit_code": r.returncode,
                 "entry": entry,
                 "pass": (r.returncode == 0 and entry["path"] == "/posix/p2"
                          and entry.get("group") == "g" and entry.get("about") == "a")})

    return {"cases": rows, "pass": all(row["pass"] for row in rows)}


# --- v0.9.0: the config is a MACHINE file, and the move is seamless ----------

def machine_config(tmp: Path) -> dict:
    """Grade the release's own claim: config left the vault, and nobody lost a
    setting on the way out.

    The vault travels between machines and every absolute path in it is wrong
    on arrival, which is why the config now lives at `~/.singularity.json`. A
    relocation is only worth shipping if it is invisible: an existing install
    must keep its values without being told to do anything, and a machine that
    already has its own config must never be overwritten by a vault that
    arrives carrying someone else's."""
    rows = []

    # (1) an upgrading machine: legacy config in the vault, nothing in home
    home = tmp / "mc-fresh-home"
    vault = tmp / "mc-fresh-vault"
    (vault / ".mb").mkdir(parents=True)
    legacy = vault / ".mb" / "config.json"
    legacy.write_text(json.dumps({
        "excerpt_chars": 200,                       # a decision: != default 150
        "min_score": 0.30,                          # a default, must not persist
        "projects": {"p": {"path": "/kept", "group": "g"}},
    }, indent=2), encoding="utf-8")
    home.mkdir()
    r = _mb(["config", "get", "excerpt_chars"], vault, home)
    machine_file = home / ".singularity.json"
    data = (json.loads(machine_file.read_text(encoding="utf-8"))
            if machine_file.exists() else {})
    kept = sorted(p.name for p in (vault / ".mb").glob("config.json.migrated-*"))
    rows.append({
        "case": "upgrade migrates the vault config into the machine file",
        "exit_code": r.returncode,
        "stdout": (r.stdout or "").strip()[:40],
        "machine_file_created": machine_file.exists(),
        "excerpt_chars": data.get("excerpt_chars"),
        "projects_kept": data.get("projects"),
        "vault_path_bound": data.get("vault_path") == str(vault),
        "legacy_file_gone": not legacy.exists(),
        "legacy_preserved_as": kept,
        "pass": (r.returncode == 0 and machine_file.exists()
                 and data.get("excerpt_chars") == 200
                 and data.get("projects", {}).get("p", {}).get("path") == "/kept"
                 and data.get("vault_path") == str(vault)
                 and not legacy.exists() and len(kept) == 1),
    })

    # (2) decisions, INCLUDING the ones that agree with the default (v0.11.2).
    #
    #     Until v0.11.2 this case asserted the opposite — a value equal to
    #     `DEFAULT_CONFIG` was dropped so the file "stays readable instead of
    #     listing forty keys". The file still does, but for the right reason:
    #     nothing injects defaults INTO `data`, so what nobody set is simply not
    #     there. Dropping them additionally made a value impossible to PIN —
    #     while `hybrid_search` defaulted to false, setting it to false deleted
    #     the key — which is what voided the v0.6.3 promise about
    #     `vault_guard_enabled` for every sparse config.
    #
    #     So the graded promise flips: a key the migrated file carried survives,
    #     default-valued or not, and the file is still short.
    rows.append({
        "case": "the migrated file records every decision, default-valued or not",
        "keys": sorted(data),
        "min_score_persisted": "min_score" in data,
        "pass": ("min_score" in data and "excerpt_chars" in data
                 and len(data) < len(DEFAULT_CONFIG)),
    })

    # (3) a configured machine meets a vault that still carries a legacy config
    home2 = tmp / "mc-configured-home"
    vault2 = tmp / "mc-configured-vault"
    (vault2 / ".mb").mkdir(parents=True)
    legacy2 = vault2 / ".mb" / "config.json"
    legacy2.write_text(json.dumps({"excerpt_chars": 999}, indent=2),
                       encoding="utf-8")
    home2.mkdir()
    (home2 / ".singularity.json").write_text(json.dumps({
        "vault_path": str(vault2), "excerpt_chars": 300}, indent=2),
        encoding="utf-8")
    r = _mb(["config", "get", "excerpt_chars"], vault2, home2)
    after = json.loads((home2 / ".singularity.json").read_text(encoding="utf-8"))
    rows.append({
        "case": "a configured machine is not overwritten by an arriving vault",
        "exit_code": r.returncode,
        "stdout": (r.stdout or "").strip()[:40],
        "machine_value": after.get("excerpt_chars"),
        "legacy_untouched": legacy2.exists(),
        "pass": (r.returncode == 0 and after.get("excerpt_chars") == 300
                 and "300" in (r.stdout or "") and legacy2.exists()),
    })

    # (4) nothing shipped names a drive: the default that survived one machine's
    #     move to another letter and shipped a dead path to everyone else
    # `C:\Windows\...` is a property of the OS, not of a machine: it is the same
    # on every Windows install and there is nothing to configure. Everything
    # else with a drive in it is one machine's layout compiled into the product.
    drive_default = re.compile(r"""["'][A-Za-z]:[\\/]+([A-Za-z0-9_. -]*)""")
    offenders = []
    for path in sorted((REPO / "singularity").rglob("*.py")) + \
            sorted((REPO / "install").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        hits = [m.group(0) for m in drive_default.finditer(text)
                if m.group(1).lower() != "windows"]
        if hits:
            offenders.append({"file": str(path.relative_to(REPO)).replace("\\", "/"),
                              "hits": sorted(set(hits))[:4]})
    rows.append({
        "case": "no shipped module names a drive letter",
        "offenders": offenders,
        "pass": not offenders,
    })

    return {"cases": rows, "pass": all(row["pass"] for row in rows)}


def run_pytest(target: str) -> dict:
    r = subprocess.run([sys.executable, "-m", "pytest", target, "-q",
                        "--ignore=tests/test_gemini_e2e.py"],
                       cwd=str(REPO), capture_output=True, text=True)
    tail = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln]
    return {"target": target, "exit_code": r.returncode,
            "summary": tail[-1].strip() if tail else "<no summary>"}


def cli_window(tmp: Path) -> dict:
    """Grade v0.10.0's own claim: the window narrows the VIEW and moves nothing.

    A facade is the kind of change that looks fine in a demo and rots in the
    gaps — a grouped path that quietly diverges from its direct one, a help
    text listing a command the parser lost, an exit code that changed without
    anyone deciding it. Each row below is a question a reporter would otherwise
    answer by hand, and the equivalence rows are byte-for-byte on purpose: a
    group that "does the same thing" is not a claim, it is a diff.
    """
    from singularity import facade
    from singularity.cli import _build_parser

    home, vault = tmp / "win-home", tmp / "win-vault"
    home.mkdir(), vault.mkdir()
    rows: list[dict] = []

    visible = [name for _, names in facade.WINDOW for name in names]
    help_out = _mb(["--help"], vault, home)
    listed = {ln.split()[0] for ln in help_out.stdout.splitlines()
              if ln.startswith("  ") and ln.strip()}
    rows.append({
        "case": "the window lists exactly the commands the facade declares",
        "exit_code": help_out.returncode,
        "visible": visible,
        "pass": help_out.returncode == 0 and set(visible) <= listed,
    })

    all_out = _mb(["help", "--all"], vault, home)
    commands = sorted(facade.parser_commands(_build_parser()))
    missing = [c for c in commands if c not in all_out.stdout]
    rows.append({
        "case": "`mb help --all` names every command the parser has",
        "exit_code": all_out.returncode,
        "commands": len(commands), "missing": missing,
        "pass": all_out.returncode == 0 and not missing,
    })

    # Equivalence, not similarity: the grouped and hosted forms must produce
    # the SAME help as the direct command, because they are meant to be the
    # same code reached through a rewritten argv.
    for label, direct, through in (
            ("group", ["daemon", "--help"], ["service", "daemon", "--help"]),
            ("group", ["config", "--help"], ["setup", "config", "--help"]),
            ("host", ["nightly", "--help"], ["curator", "nightly", "--help"]),
            ("host", ["stats", "--help"], ["status", "stats", "--help"])):
        a, b = _mb(direct, vault, home), _mb(through, vault, home)
        rows.append({
            "case": f"{label}: `mb {' '.join(through)}` == `mb {' '.join(direct)}`",
            "exit_codes": [a.returncode, b.returncode],
            "identical_stdout": a.stdout == b.stdout,
            "pass": a.returncode == b.returncode == 0 and a.stdout == b.stdout,
        })

    # A host still runs itself: `mb curator` must reach the report, not a
    # listing. No vault here, so 2 with the vault diagnostic is the correct
    # answer — what would fail the release is argparse rejecting the command.
    host = _mb(["curator"], vault, home)
    rows.append({
        "case": "a host still dispatches to its own command",
        "exit_code": host.returncode,
        "rejected_by_parser": "invalid choice" in (host.stderr or ""),
        "pass": "invalid choice" not in (host.stderr or ""),
    })

    # The one behavioural change this release makes, pinned so a later edit
    # cannot move it silently (docs/UPGRADE.md states these three).
    codes = {
        "mb help": _mb(["help"], vault, home).returncode,
        "mb <unknown>": _mb(["nonsense"], vault, home).returncode,
        "mb service <unknown>": _mb(["service", "nonsense"], vault, home).returncode,
    }
    rows.append({
        "case": "exit codes: help is not an error, unknown still is",
        "codes": codes,
        "pass": codes["mb help"] == 0 and codes["mb <unknown>"] == 2
        and codes["mb service <unknown>"] == 2,
    })

    return {"pass": all(r["pass"] for r in rows), "rows": rows}


def package_layout() -> dict:
    """Grade v0.11.0's own risk: 22 modules moved into bounded contexts.

    An editable install hides the one way that goes wrong — `pip install`
    building a wheel that omits a new subpackage. The engine would import fine
    on the developer's machine and fail on the next one with
    `ModuleNotFoundError`, which is exactly the class of defect a baseline
    exists to catch before a user does.
    """
    import importlib

    from setuptools import find_packages

    # v0.18.0 adds the code graph package and the platform lock every writer
    # lock now goes through; the hooks import codegraph.sidecar on every
    # SessionStart, so a wheel without it breaks the first session, not a command
    expected = ["singularity.knowledge", "singularity.machine",
                "singularity.telemetry", "singularity.commands",
                "singularity.codegraph", "singularity.platform"]
    discovered = set(find_packages(where=str(REPO), include=["singularity*"]))
    rows = [{"package": name, "discovered": name in discovered} for name in expected]

    # v0.19.0: the jvm family's modules; the two extractors import their
    # pinned grammars at module level, so importing them proves the `graph`
    # extra carries Java and Kotlin on this machine
    imports = []
    for module in ("singularity.knowledge.sources", "singularity.machine.status",
                   "singularity.telemetry.reflect", "singularity.commands.memory",
                   "singularity.render", "singularity.contracts",
                   "singularity.search.lexical", "singularity.search.metrics",
                   "singularity.codegraph.sidecar", "singularity.platform.fslock",
                   "singularity.codegraph.languages", "singularity.codegraph.extract_java",
                   "singularity.codegraph.extract_kotlin", "singularity.codegraph.resolve_jvm"):
        try:
            importlib.import_module(module)
            imports.append({"module": module, "ok": True})
        except Exception as exc:                            # noqa: BLE001
            imports.append({"module": module, "ok": False, "error": str(exc)})

    return {"expected_packages": rows, "imports": imports,
            "package_root_files": sorted(p.name for p in (REPO / "singularity").glob("*.py")),
            "pass": all(r["discovered"] for r in rows) and all(i["ok"] for i in imports)}


def main() -> None:
    pkg = Path(singularity.__file__).resolve().parent
    if pkg.parent != REPO:
        raise SystemExit(f"refusing to snap: imported {pkg}, repo is {REPO}")

    cfg = load_config()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        shims = shim_matrix(tmp)
        locator = locator_matrix(tmp)
        config_write = nested_config_write(tmp)
        machine_cfg = machine_config(tmp)
        window = cli_window(tmp)
    layout = package_layout()

    snap = {
        "taken_at": datetime.now().isoformat(timespec="seconds"),
        "note": "v0.19.0 baseline: the same eight surfaces, taken on the release merge commit. The release teaches the code graph Java and Kotlin (one jvm family) and rebuilds every graph once (schema 6, resolver 6); it adds no doctor check and no package, so what a snapshot can see of it is K2's detail and the jvm modules importing with their pinned grammars. It also stops the scheduler's jobs from flashing a console window per child process on Windows, which no surface here measures (tests/test_no_console_windows.py does)",
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
        "config_write_path": config_write,
        "v090_machine_config": machine_cfg,
        "v0100_cli_window": window,
        "v0110_package_layout": layout,
        "tests": [run_pytest("tests/")],
    }
    snap["all_pass"] = (
        shims["pass"]
        and locator["pass"]
        and config_write["pass"]
        and machine_cfg["pass"]
        and window["pass"]
        and layout["pass"]
        and snap["issue_12_live_config_reload"].get("pass", None) is not False
        and all(t["exit_code"] == 0 for t in snap["tests"])
    )
    print(json.dumps(snap, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
