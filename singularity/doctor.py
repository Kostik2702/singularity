"""mb doctor — installation verification (port of genevra-setup-doctor).

Statuses: PASS / WARN / FAIL / FIXED / SKIP / AUTH. Guarded checks (one
failure never aborts the run), exit 0 unless there is a hard FAIL.
Full specification: vault 04_Plans/Doctor_Plan. This is the M0-M2 subset:
blocks A (structure/config), B (search core), C (scheduler), D (claude/CLI).
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .platform import current_os, scheduler

MEMORY_FOLDERS = ["learnings/active", "learnings/archive", "decisions", "howto",
                  "incidents", "rules", "reference", "changelog", "templates", "_moc"]


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)
    quiet: bool = False   # suppress human lines (used by --json)

    def add(self, check_id: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append({"id": check_id, "status": status,
                            "detail": detail, "fix": fix})
        if not self.quiet:
            print(f"[{status}] {check_id}: {detail}")

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.checks:
            out[c["status"]] = out.get(c["status"], 0) + 1
        return out

    @property
    def has_fail(self) -> bool:
        return any(c["status"] == "FAIL" for c in self.checks)


def _guard(rep: Report, check_id: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — doctor checks are guarded by design
        rep.add(check_id, "FAIL", f"check crashed: {exc}")


def _load_install(name: str):
    """Load a module from install/ (not an importable package) for its constants
    and safe helpers — the single source, so doctor can't drift from the registrar."""
    import importlib.util
    engine = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, engine / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _action_matches(state: dict, expected_args: str | None) -> bool:
    """Does the scheduler's ACTUAL stored action match the argv we intended?
    Returns True when uncomparable (non-Windows read-back) so we never raise a
    false alarm; False only on a concrete mismatch (e.g. a vault path that lost
    its quoting and was split — the exact pre-AUD-01 breakage)."""
    actual = state.get("arguments")
    if expected_args is None or actual is None:
        return True
    return actual.strip() == expected_args.strip()


def _run_shim(sh: str, shim: Path) -> tuple[int, str, str]:
    """Run the POSIX shim through sh; separated so tests can stub the subprocess."""
    proc = subprocess.run([sh, str(shim), "--version"],
                          capture_output=True, text=True, timeout=60)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


# Watcher is considered alive if its heartbeat file was touched this recently.
WATCH_HEARTBEAT_MAX_AGE_S = 15 * 60


def gemini_block(rep: Report, fix: bool) -> None:
    """Block E: verify the Gemini integration on a WORKING path (version, managed
    profile, portable skill targets, settings parse, named hook), not mere file
    presence. `--fix` runs ONLY the safe registrar (no network/auth). Absent client
    is a single SKIP. Module-level (not a run() closure) so it is unit-testable."""
    rg = _load_install("register_gemini")
    home_g = Path.home() / ".gemini"
    if not (home_g.exists() or shutil.which("gemini") is not None):
        rep.add("E1-gemini", "SKIP", "Gemini CLI not installed on this machine")
        return
    version = rg._gemini_version()

    def profile_ok() -> bool:
        p = home_g / "GEMINI.md"
        return p.exists() and rg.BEGIN in p.read_text(encoding="utf-8", errors="ignore")

    def missing_skills() -> list[str]:
        d = home_g / "skills"
        return [n for n in rg.GEMINI_SKILLS if not (d / n).exists()]

    def hook_state():
        s = home_g / "settings.json"
        if not s.exists():
            return False
        try:
            data = json.loads(s.read_text(encoding="utf-8-sig"))   # BOM-tolerant
        except json.JSONDecodeError:
            return "malformed"
        hooks = data.get("hooks", {})

        def _names(event):
            return [h.get("name") for dfn in hooks.get(event, []) for h in dfn.get("hooks", [])]

        return (rg.SESSION_START_HOOK_NAME in _names("SessionStart")
                and rg.AFTER_AGENT_HOOK_NAME in _names("AfterAgent"))

    supported = version is not None and version >= rg.MIN_HOOKS_VERSION

    # --fix runs ONLY the safe registrar (no network/auth) once, then re-checks.
    fixed = False
    if fix and (not profile_ok() or missing_skills()
                or (supported and hook_state() is not True)):
        rg.main()
        fixed = True

    if version is None:
        rep.add("E1-gemini", "WARN", "installed, but `gemini --version` unreadable")
    elif not supported:
        rep.add("E1-gemini", "WARN",
                f"gemini {'.'.join(map(str, version))} < "
                f"{'.'.join(map(str, rg.MIN_HOOKS_VERSION))} (skills/hooks may be ignored)")
    else:
        rep.add("E1-gemini", "PASS", f"gemini {'.'.join(map(str, version))}")

    if profile_ok():
        rep.add("E2-gemini-profile", "FIXED" if fixed else "PASS",
                "managed GEMINI.md block present")
    else:
        rep.add("E2-gemini-profile", "WARN", "managed GEMINI.md block missing",
                fix="python install/register_gemini.py")

    miss = missing_skills()
    if not miss:
        rep.add("E3-gemini-skills", "FIXED" if fixed else "PASS",
                f"{len(rg.GEMINI_SKILLS)} portable skills deployed")
    else:
        rep.add("E3-gemini-skills", "WARN", f"skills not deployed: {', '.join(miss)}",
                fix="python install/register_gemini.py")

    h = hook_state()
    if h == "malformed":
        rep.add("E4-gemini-hooks", "FAIL", "~/.gemini/settings.json unparseable",
                fix="repair or remove the file, then mb doctor --fix")
    elif not supported:
        rep.add("E4-gemini-hooks", "SKIP",
                f"SessionStart hook requires gemini >= {'.'.join(map(str, rg.MIN_HOOKS_VERSION))}")
    elif h is True:
        rep.add("E4-gemini-hooks", "FIXED" if fixed else "PASS",
                "SessionStart + AfterAgent hooks registered")
    else:
        rep.add("E4-gemini-hooks", "WARN", "SessionStart/AfterAgent hooks not registered",
                fix="python install/register_gemini.py")


def run(cfg: Config, fix: bool = False, heavy: bool = False,
        as_json: bool = False) -> int:
    rep = Report(quiet=as_json)

    # ---- Block A: structure + config ---------------------------------
    def a1():
        rep.add("A1-config", "PASS", str(cfg.config_path))

    def a2():
        if (cfg.vault / ".obsidian").exists():
            rep.add("A2-vault-obsidian", "PASS", str(cfg.vault))
        else:
            rep.add("A2-vault-obsidian", "WARN",
                    "no .obsidian/ — vault not opened in Obsidian yet")

    def a3():
        missing = [f for f in MEMORY_FOLDERS if not (cfg.vault / f).exists()]
        if not missing:
            rep.add("A3-folders", "PASS", "memory folder tree complete")
        elif fix:
            for f in missing:
                (cfg.vault / f).mkdir(parents=True, exist_ok=True)
            rep.add("A3-folders", "FIXED", f"created: {', '.join(missing)}")
        else:
            rep.add("A3-folders", "WARN", f"missing: {', '.join(missing)}",
                    fix="mb doctor --fix")

    def a4():
        if cfg.mb_dir.exists():
            rep.add("A4-mb-dir", "PASS", str(cfg.mb_dir))
        else:
            cfg.mb_dir.mkdir(parents=True, exist_ok=True)
            rep.add("A4-mb-dir", "FIXED", "created .mb/")

    def a5():
        from .platform import secrets
        be = secrets.backend()
        if be == "keyring":
            rep.add("A5-secrets", "PASS", "secure keyring backend")
        else:
            rep.add("A5-secrets", "WARN",
                    "no OS keyring — secrets require explicit plaintext opt-in",
                    fix="install a keyring backend for secure secret storage")

    _guard(rep, "A1", a1)
    _guard(rep, "A2", a2)
    _guard(rep, "A3", a3)
    _guard(rep, "A4", a4)
    _guard(rep, "A5", a5)

    # ---- Block B: semantic search core --------------------------------
    def b2():
        missing = []
        for mod in ("numpy", "yaml", "watchdog", "psutil"):
            try:
                importlib.import_module(mod)
            except ImportError:
                missing.append(mod)
        try:
            importlib.import_module("sentence_transformers")
        except ImportError:
            missing.append("sentence-transformers")
        if missing:
            rep.add("B2-deps", "FAIL", f"missing: {', '.join(missing)}",
                    fix="pip install -r requirements.txt")
        else:
            rep.add("B2-deps", "PASS", "python deps importable")

    def b3():
        from .search import client, model_store

        state = model_store.status(cfg)
        if state["installed"]:
            rep.add("B3-model", "PASS",
                    f"{state['model']} @ {state.get('revision', '?')} "
                    f"({state.get('snapshot', '?')})")
            return
        if fix and heavy:
            installed = model_store.install(cfg)
            client.stop_daemon(cfg)
            daemon_ok = client.start_daemon(cfg)
            rep.add("B3-model", "FIXED" if daemon_ok else "FAIL",
                    f"{installed.get('source', 'installed')}; "
                    f"daemon={'restarted' if daemon_ok else 'restart failed'}",
                    fix="" if daemon_ok else "mb daemon start")
            return
        rep.add("B3-model", "FAIL",
                f"local embedding model unavailable: {state.get('reason', 'unknown')}; "
                f"cache={state.get('cache', cfg.hf_cache)}",
                fix="mb model install  (or mb doctor --fix --heavy)")

    def b4():
        from .search import indexer

        files = indexer.index_files(cfg.memory_index_dir)
        if files is not None:
            emb, meta = files
            data = json.loads(meta.read_text(encoding="utf-8"))
            chunks = data.get("chunks", [])
            n = len(chunks) if isinstance(chunks, list) else 0
            if n > 0:
                age_h = (time.time() - emb.stat().st_mtime) / 3600
                status = "PASS" if age_h < 48 else "WARN"
                rep.add("B4-index", status,
                        f"{n} chunks, model={data.get('model','?')}, age={age_h:.0f}h")
                return
        if fix and heavy:
            from .search import client
            stats = client.run_index(cfg, "rebuild")
            rep.add("B4-index", "FIXED", f"rebuilt: {stats}")
        else:
            rep.add("B4-index", "FAIL", "memory index missing/empty",
                    fix="mb index rebuild  (or mb doctor --fix --heavy)")

    def b7():
        from .search import client
        pong = client.ping(cfg)
        if pong:
            rep.add("B7-daemon", "PASS", f"ping ok, pid={pong.get('pid')}")
        elif fix:
            ok = client.start_daemon(cfg)
            rep.add("B7-daemon", "FIXED" if ok else "FAIL",
                    "started" if ok else "start failed — see .mb/daemon.log")
        else:
            rep.add("B7-daemon", "WARN", "daemon not running",
                    fix="mb daemon start")

    def b8():
        from .search import client
        if not client.ping(cfg):
            rep.add("B8-smoke", "SKIP", "daemon down")
            return
        resp = client.search(cfg, {"cmd": "search", "query": "memory brain",
                                   "top_k": 1, "format": "json"})
        n = len(resp.get("results", []))
        rep.add("B8-smoke", "PASS" if n else "WARN",
                f"'memory brain' -> {n} hits, {resp.get('time_ms','?')}ms")

    def b9():
        from .search import client
        st = client.status(cfg)
        if st.get("orphan"):
            if fix:
                client._clear_state(cfg)
                rep.add("B9-daemon-identity", "FIXED",
                        "orphan daemon state (process gone) cleaned")
            else:
                rep.add("B9-daemon-identity", "WARN",
                        f"orphan daemon state: pid {st.get('pid')} not alive",
                        fix="mb doctor --fix")
        else:
            rep.add("B9-daemon-identity", "PASS",
                    f"no orphan (instance={str(st.get('instance_id'))[:8] or '-'})")

    _guard(rep, "B2", b2)
    _guard(rep, "B3", b3)
    _guard(rep, "B4", b4)
    _guard(rep, "B7", b7)
    _guard(rep, "B8", b8)
    _guard(rep, "B9", b9)

    # ---- Block C: scheduler -------------------------------------------
    def c1():
        from . import nightly

        job = nightly.nightly_job(cfg)
        state = scheduler.verify(job)
        if not state.get("registered"):
            if fix:
                # migrate_scheduler_job also removes the legacy standalone
                # "nightly-index" job if still present (T12) — single source
                # shared with the installer, so the two paths can't drift.
                res = nightly.migrate_scheduler_job(cfg)
                rep.add("C1-nightly", "FIXED" if res.startswith("OK") else "FAIL", res)
            else:
                rep.add("C1-nightly", "WARN", "nightly job not registered",
                        fix="mb doctor --fix")
            return
        # Registered — but is the STORED action the argv we intended? A task
        # whose --vault path was split apart still shows "registered"; that must
        # not read as healthy.
        if not _action_matches(state, scheduler.expected_arguments(job)):
            if fix:
                res = nightly.migrate_scheduler_job(cfg)
                rep.add("C1-nightly", "FIXED" if res.startswith("OK") else "FAIL",
                        f"stale/broken action re-registered: {res}")
            else:
                rep.add("C1-nightly", "FAIL",
                        f"registered action != expected argv (stored: {state.get('arguments')!r})",
                        fix="mb doctor --fix")
            return
        status = "PASS" if state.get("coalescing") else "WARN"
        rep.add("C1-nightly", status,
                f"registered + action verified ({current_os()}), "
                f"coalescing={state.get('coalescing')}")

    def c2():
        from . import watch as watch_mod

        # The login task must be registered AND its STORED action must be the
        # argv we intended — a watch task whose --vault path lost its quoting
        # (pre-AUD-01 registration) exits 1 at every login and is otherwise
        # visible only as "no heartbeat" (incident 2026-07-20).
        job = watch_mod.watch_job(cfg)
        state = scheduler.verify(job)
        problem = None
        if not state.get("registered"):
            problem = ("WARN", "watch login task not registered")
        elif not _action_matches(state, scheduler.expected_arguments(job)):
            problem = ("FAIL",
                       f"registered action != expected argv (stored: {state.get('arguments')!r})")
        if problem:
            if fix:
                res = scheduler.register(job)
                rep.add("C2-watcher", "FIXED" if res.startswith("OK") else "FAIL",
                        f"{problem[1]} -> re-registered: {res} "
                        "(starts at next login; `mb watch` to start now)")
            else:
                rep.add("C2-watcher", problem[0], problem[1], fix="mb doctor --fix")
            return
        # Registration verified — now liveness via the heartbeat file.
        hb = cfg.watch_heartbeat_file
        if not hb.exists():
            rep.add("C2-watcher", "WARN", "no watcher heartbeat (watcher not running?)",
                    fix="mb watch  (or check login task)")
            return
        try:
            age = time.time() - float(hb.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rep.add("C2-watcher", "WARN", "unreadable watcher heartbeat")
            return
        if age <= WATCH_HEARTBEAT_MAX_AGE_S:
            rep.add("C2-watcher", "PASS", f"watcher heartbeat {age:.0f}s ago")
        else:
            rep.add("C2-watcher", "WARN",
                    f"watcher heartbeat stale ({age/60:.0f}min)", fix="restart mb watch")

    def c5():
        if cfg.lock_dir.exists():
            age_h = (time.time() - cfg.lock_dir.stat().st_mtime) / 3600
            if age_h > 6:
                if fix:
                    shutil.rmtree(cfg.lock_dir, ignore_errors=True)
                    rep.add("C5-lock", "FIXED", f"stale lock ({age_h:.0f}h) removed")
                else:
                    rep.add("C5-lock", "WARN", f"stale lock ({age_h:.0f}h)")
            else:
                rep.add("C5-lock", "WARN", "rebuild in progress?")
        else:
            rep.add("C5-lock", "PASS", "no stale lock")

    _guard(rep, "C1", c1)
    _guard(rep, "C2", c2)
    _guard(rep, "C5", c5)

    # ---- Block D: host tools ------------------------------------------
    def d4():
        missing = [t for t in ("git",) if shutil.which(t) is None]
        if missing:
            rep.add("D4-cli", "FAIL", f"missing on PATH: {missing}")
        else:
            rep.add("D4-cli", "PASS", "git on PATH")

    def d2():
        # every engine-owned agent (singularity/agents/*.md) must be registered
        # in ~/.claude/agents so councils can spawn them by portable name.
        src_dir = Path(__file__).resolve().parent / "agents"
        owned = sorted(p.stem for p in src_dir.glob("*.md")) if src_dir.is_dir() else []
        if not owned:
            rep.add("D2-agents", "SKIP", "no engine agents/ dir")
            return
        dest = Path.home() / ".claude" / "agents"
        missing = [n for n in owned if not (dest / f"{n}.md").exists()]
        if not missing:
            rep.add("D2-agents", "PASS", f"{len(owned)} engine agents registered")
        else:
            rep.add("D2-agents", "WARN", f"agents not registered: {', '.join(missing)}",
                    fix="python install/register_agents.py")

    def d1():
        # T02: desired/installed/effective capture-hooks state across every
        # client, not just a crude "singularity" substring check on Claude.
        # An intentionally disabled capture toggle is a deliberate user choice,
        # never a failure — only a missing registration is worth a WARN.
        from . import hooks_state

        state = hooks_state.status(cfg.vault)
        if state["source"] == "error":
            rep.add("D1-hooks", "WARN",
                    f"capture hooks config unreadable, treated as default: "
                    f"{state['warnings'][0] if state['warnings'] else '?'}",
                    fix="fix or remove .mb/config.json, then mb hooks status")
            return
        desired = "enabled" if state["configured"] else "disabled"
        clients = state["clients"]
        installed_any = any(c["installed"] for c in clients.values())
        detail = (f"capture hooks {desired} (default "
                  f"{'enabled' if state['default'] else 'disabled'}); " +
                  ", ".join(f"{n}={'installed' if c['installed'] else 'not-installed'}"
                           for n, c in clients.items()))
        if not installed_any:
            rep.add("D1-hooks", "WARN", detail + " — no client has the hook registered",
                    fix="python install/register_hooks.py")
        elif not state["configured"]:
            rep.add("D1-hooks", "PASS", detail + " — intentionally disabled, not a failure")
        else:
            rep.add("D1-hooks", "PASS", detail)

    def d3():
        # T07: module-context PreToolUse hook registration (Claude only, v1).
        settings = Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            rep.add("D3-module-context", "SKIP", "no ~/.claude/settings.json")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            rep.add("D3-module-context", "WARN", "settings.json unparseable")
            return
        pre = data.get("hooks", {}).get("PreToolUse", []) if isinstance(data, dict) else []
        installed = False
        if isinstance(pre, list):
            for entry in pre:
                if not isinstance(entry, dict):
                    continue
                if entry.get("_mb"):
                    installed = True
                    break
                for h in entry.get("hooks", []):
                    if isinstance(h, dict) and "module_context.py" in str(h.get("command", "")):
                        installed = True
                        break
        from .config import read_config_tolerant
        enabled = bool(read_config_tolerant(cfg.vault).get("module_context_enabled", False))
        state = "enabled" if enabled else "disabled (default, opt-in via module_context_enabled)"
        if installed:
            rep.add("D3-module-context", "PASS", f"PreToolUse hook registered, {state}")
        else:
            rep.add("D3-module-context", "WARN", f"module-context hook not registered, {state}",
                    fix="python install/register_hooks.py")

    def d5():
        # T08: vault-guard PreToolUse hook registration (Claude only, v1).
        settings = Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            rep.add("D5-vault-guard", "SKIP", "no ~/.claude/settings.json")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            rep.add("D5-vault-guard", "WARN", "settings.json unparseable")
            return
        pre = data.get("hooks", {}).get("PreToolUse", []) if isinstance(data, dict) else []
        installed = False
        if isinstance(pre, list):
            for entry in pre:
                if not isinstance(entry, dict):
                    continue
                if entry.get("_mb"):
                    installed = True
                    break
                for h in entry.get("hooks", []):
                    if isinstance(h, dict) and "vault_guard.py" in str(h.get("command", "")):
                        installed = True
                        break
        from .config import read_config_tolerant
        light_cfg = read_config_tolerant(cfg.vault)
        enabled = bool(light_cfg.get("vault_guard_enabled", False))
        mode = light_cfg.get("vault_guard_mode", "audit")
        state = f"mode={mode}" if enabled else "disabled (default, opt-in via vault_guard_enabled)"
        if installed:
            rep.add("D5-vault-guard", "PASS", f"PreToolUse hook registered, {state}")
        else:
            rep.add("D5-vault-guard", "WARN", f"vault-guard hook not registered, {state}",
                    fix="python install/register_hooks.py")

    def d6():
        # incident 2026-07-14 "mb POSIX shim silently dies in Git Bash": the
        # extensionless bin/mb shadows mb.cmd in Git Bash, so it must actually
        # run there — exit 0 with non-empty output, never a silent 127.
        shim = Path(__file__).resolve().parents[1] / "bin" / "mb"
        if not shim.exists():
            rep.add("D6-posix-shim", "SKIP", "no bin/mb POSIX shim")
            return
        sh = shutil.which("sh") or shutil.which("bash")
        if sh is None:
            rep.add("D6-posix-shim", "SKIP", "no sh/bash on PATH (POSIX shim unused)")
            return
        code, out, err = _run_shim(sh, shim)
        if code == 0 and out:
            rep.add("D6-posix-shim", "PASS", f"`sh bin/mb --version` -> {out}")
        else:
            detail = (f"`sh bin/mb --version` exit {code}, "
                      f"stdout {'empty' if not out else repr(out)}")
            if err:
                detail += f", stderr: {err[:120]}"
            rep.add("D6-posix-shim", "FAIL", detail,
                    fix="bin/mb must pick the interpreter via [ -x ... ] and fail loud")

    _guard(rep, "D4", d4)
    _guard(rep, "D2", d2)
    _guard(rep, "D1", d1)
    _guard(rep, "D3", d3)
    _guard(rep, "D5", d5)
    _guard(rep, "D6", d6)

    # ---- Block E: Gemini CLI integration (GCLI-05) --------------------
    _guard(rep, "E-gemini", lambda: gemini_block(rep, fix))

    # ---- summary -------------------------------------------------------
    counts = rep.counts()
    payload = {"counts": counts, "checks": rep.checks, "ok": not rep.has_fail}
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    (cfg.mb_dir / "doctor-last.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if as_json:
        # exactly one machine-readable document on stdout, nothing else
        print(json.dumps(payload, ensure_ascii=False))
        return 1 if rep.has_fail else 0

    print("\n================ mb doctor summary ================")
    print(" | ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    fails = [c for c in rep.checks if c["status"] == "FAIL"]
    for c in fails:
        print(f"  FAIL {c['id']}: {c['detail']}" + (f"  fix: {c['fix']}" if c["fix"] else ""))
    print("===================================================")
    return 1 if rep.has_fail else 0
