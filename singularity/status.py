"""mb status — one unified health snapshot (AUD-18).

Aggregates the health signals other subsystems already expose (index, daemon
identity, watcher heartbeat, scheduler, disk) into a single human/JSON report.
Never prints query content or secrets. Guarded: one failing probe degrades to
an "error" field, it never aborts the snapshot.
"""
from __future__ import annotations

import json
import time
from typing import Any

from .config import Config


def _guard(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — a probe failure must not sink status
        return {"error": f"{type(exc).__name__}: {exc}"}


def _index(cfg: Config) -> dict[str, Any]:
    from .search import indexer
    files = indexer.index_files(cfg.memory_index_dir)
    if files is None:
        return {"present": False}
    emb_path, meta_path = files
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    age_h = (time.time() - emb_path.stat().st_mtime) / 3600
    return {"present": True, "chunks": meta.get("chunk_count"),
            "model": meta.get("model"), "chunk_schema": meta.get("chunk_schema"),
            "age_hours": round(age_h, 1),
            "layout": "generation" if "gen_" in str(emb_path) else "legacy-flat"}


def _watcher(cfg: Config) -> dict[str, Any]:
    from .watch import HEARTBEAT_FILE
    hb = cfg.mb_dir / HEARTBEAT_FILE
    if not hb.exists():
        return {"alive": False}
    age = time.time() - float(hb.read_text(encoding="utf-8"))
    return {"alive": age <= 15 * 60, "heartbeat_age_s": round(age, 1)}


def _scheduler(cfg: Config) -> dict[str, Any]:
    from .platform import scheduler
    out = {}
    for name, kw in (("nightly-index", {"daily_at": "03:00"}),
                     ("watch", {"at_login": True})):
        args = ["-m", "singularity", "--vault", str(cfg.vault),
                "index", "update"] if name == "nightly-index" else \
               ["-m", "singularity", "--vault", str(cfg.vault), "watch"]
        st = scheduler.verify(scheduler.Job(name, args, **kw))
        out[name] = {"registered": st.get("registered", False),
                     "coalescing": st.get("coalescing")}
    return out


def _disk(cfg: Config) -> dict[str, Any]:
    total = 0
    if cfg.mb_dir.exists():
        for p in cfg.mb_dir.rglob("*"):
            if p.is_file():
                try:
                    total += p.stat().st_size
                except OSError:
                    pass
    return {"mb_dir_mb": round(total / 1e6, 1)}


def snapshot(cfg: Config) -> dict[str, Any]:
    from .search import client
    return {
        "vault": str(cfg.vault),
        "index": _guard(lambda: _index(cfg)),
        "daemon": _guard(lambda: client.status(cfg)),
        "watcher": _guard(lambda: _watcher(cfg)),
        "scheduler": _guard(lambda: _scheduler(cfg)),
        "disk": _guard(lambda: _disk(cfg)),
    }


def run(cfg: Config, as_json: bool = False) -> int:
    snap = snapshot(cfg)
    if as_json:
        print(json.dumps(snap, ensure_ascii=False))
        return 0
    print(f"vault: {snap['vault']}")
    idx = snap["index"]
    if idx.get("present"):
        print(f"index: {idx.get('chunks')} chunks, {idx.get('layout')}, "
              f"age {idx.get('age_hours')}h, schema {idx.get('chunk_schema')}")
    else:
        print(f"index: {idx.get('error', 'absent')}")
    dae = snap["daemon"]
    print(f"daemon: {'up' if dae.get('ping') else 'down'} "
          f"(pid {dae.get('pid')}, orphan={dae.get('orphan')})")
    w = snap["watcher"]
    print(f"watcher: {'alive' if w.get('alive') else 'not running'}"
          + (f" (heartbeat {w.get('heartbeat_age_s')}s ago)" if w.get("heartbeat_age_s") else ""))
    for name, s in (snap["scheduler"].items() if isinstance(snap["scheduler"], dict)
                    and "error" not in snap["scheduler"] else []):
        print(f"scheduler[{name}]: registered={s.get('registered')}")
    print(f"disk(.mb): {snap['disk'].get('mb_dir_mb')} MB")
    return 0
