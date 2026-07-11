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


def _action_matches(state: dict, expected_args: str | None) -> bool:
    """Does the scheduler's ACTUAL stored action match the argv we intended?
    Returns True when uncomparable (non-Windows read-back) so we never raise a
    false alarm; False only on a concrete mismatch (e.g. a vault path that lost
    its quoting and was split — the exact pre-AUD-01 breakage)."""
    actual = state.get("arguments")
    if expected_args is None or actual is None:
        return True
    return actual.strip() == expected_args.strip()


# Watcher is considered alive if its heartbeat file was touched this recently.
WATCH_HEARTBEAT_MAX_AGE_S = 15 * 60


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

    _guard(rep, "A1", a1)
    _guard(rep, "A2", a2)
    _guard(rep, "A3", a3)
    _guard(rep, "A4", a4)

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

    def b4():
        emb = cfg.memory_index_dir / "embeddings.npy"
        meta = cfg.memory_index_dir / "metadata.json"
        if emb.exists() and meta.exists():
            data = json.loads(meta.read_text(encoding="utf-8"))
            n = data.get("chunk_count", 0)
            if n > 0:
                age_h = (time.time() - emb.stat().st_mtime) / 3600
                status = "PASS" if age_h < 48 else "WARN"
                rep.add("B4-index", status,
                        f"{n} chunks, model={data.get('model','?')}, age={age_h:.0f}h")
                return
        if heavy:
            from .search import indexer
            stats = indexer.rebuild(cfg)
            rep.add("B4-index", "FIXED", f"rebuilt: {stats}")
        else:
            rep.add("B4-index", "FAIL", "memory index missing/empty",
                    fix="mb index rebuild  (or doctor --fix --heavy)")

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
    _guard(rep, "B4", b4)
    _guard(rep, "B7", b7)
    _guard(rep, "B8", b8)
    _guard(rep, "B9", b9)

    # ---- Block C: scheduler -------------------------------------------
    def c1():
        job = scheduler.Job("nightly-index",
                            ["-m", "singularity", "--vault", str(cfg.vault),
                             "index", "update"],
                            daily_at="03:00")
        state = scheduler.verify(job)
        if not state.get("registered"):
            if fix:
                res = scheduler.register(job)
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
                res = scheduler.register(job)
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
        from .watch import HEARTBEAT_FILE
        hb = cfg.mb_dir / HEARTBEAT_FILE
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
        settings = Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            rep.add("D1-hooks", "SKIP", "no ~/.claude/settings.json (hooks not installed yet — M3)")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            rep.add("D1-hooks", "FAIL", "settings.json unparseable")
            return
        ours = "singularity" in json.dumps(data.get("hooks", {}))
        rep.add("D1-hooks", "PASS" if ours else "SKIP",
                "singularity hooks registered" if ours else "hooks not registered (M3)")

    _guard(rep, "D4", d4)
    _guard(rep, "D2", d2)
    _guard(rep, "D1", d1)

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
