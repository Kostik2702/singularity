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

    def add(self, check_id: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append({"id": check_id, "status": status,
                            "detail": detail, "fix": fix})
        line = f"[{status}] {check_id}: {detail}"
        print(line)

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


def run(cfg: Config, fix: bool = False, heavy: bool = False) -> int:
    rep = Report()

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

    _guard(rep, "B2", b2)
    _guard(rep, "B4", b4)
    _guard(rep, "B7", b7)
    _guard(rep, "B8", b8)

    # ---- Block C: scheduler -------------------------------------------
    def c1():
        job = scheduler.Job("nightly-index",
                            ["-m", "singularity", "--vault", str(cfg.vault),
                             "index", "update"],
                            daily_at="03:00")
        state = scheduler.verify(job)
        if state.get("registered"):
            status = "PASS" if state.get("coalescing") else "WARN"
            rep.add("C1-nightly", status,
                    f"registered ({current_os()}), coalescing={state.get('coalescing')}")
        elif fix:
            res = scheduler.register(job)
            rep.add("C1-nightly", "FIXED" if res.startswith("OK") else "FAIL", res)
        else:
            rep.add("C1-nightly", "WARN", "nightly job not registered",
                    fix="mb doctor --fix")

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
    _guard(rep, "C5", c5)

    # ---- Block D: host tools ------------------------------------------
    def d4():
        missing = [t for t in ("git",) if shutil.which(t) is None]
        if missing:
            rep.add("D4-cli", "FAIL", f"missing on PATH: {missing}")
        else:
            rep.add("D4-cli", "PASS", "git on PATH")

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
    _guard(rep, "D1", d1)

    # ---- summary -------------------------------------------------------
    counts = rep.counts()
    print("\n================ mb doctor summary ================")
    print(" | ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    fails = [c for c in rep.checks if c["status"] == "FAIL"]
    for c in fails:
        print(f"  FAIL {c['id']}: {c['detail']}" + (f"  fix: {c['fix']}" if c["fix"] else ""))
    print("===================================================")
    (cfg.mb_dir / "doctor-last.json").write_text(
        json.dumps({"counts": counts, "checks": rep.checks}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    return 1 if rep.has_fail else 0
