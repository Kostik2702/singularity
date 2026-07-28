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
    hb = cfg.watch_heartbeat_file
    if not hb.exists():
        return {"alive": False}
    age = time.time() - float(hb.read_text(encoding="utf-8"))
    return {"alive": age <= 15 * 60, "heartbeat_age_s": round(age, 1)}


def _scheduler(cfg: Config) -> dict[str, Any]:
    from .platform import scheduler
    from . import nightly
    out = {}
    jobs = {
        nightly.JOB_NAME: nightly.nightly_job(cfg),
        "watch": scheduler.Job("watch", ["-m", "singularity", "--vault",
                                         str(cfg.vault), "watch"], at_login=True),
    }
    for name, job in jobs.items():
        st = scheduler.verify(job)
        out[name] = {"registered": st.get("registered", False),
                     "coalescing": st.get("coalescing")}
    return out


def _backlog(cfg: Config) -> dict[str, Any]:
    from . import nightly
    path = cfg.mb_dir / nightly.DIGEST_SNAPSHOT_NAME
    if not path.exists():
        return {"present": False}
    data = json.loads(path.read_text(encoding="utf-8"))
    threshold = float(cfg.get("digest_score_threshold"))
    entries = data.get("entries", [])
    actionable = sum(1 for e in entries
                     if e.get("candidates") and e["candidates"][0]["score"] >= threshold)
    return {"present": True, "entries": len(entries), "actionable": actionable,
            "index_status": data.get("index_status")}


def _sources(cfg: Config) -> dict[str, Any]:
    """Ingest layer at a glance (W11), including the one number worth acting
    on: how many documents are still waiting to be turned into notes."""
    from . import sources as sources_mod
    from .search import indexer

    records = [r for r in sources_mod.read_registry(cfg)["sources"].values()
               if isinstance(r, dict)]
    if not records:
        return {"present": False}
    summary = indexer.index_summary(cfg.sources_index_dir) or {}
    return {"present": True, "documents": len(records),
            "unprocessed": sum(1 for r in records if r.get("status") == "new"),
            "chunks": summary.get("chunk_count", 0),
            "indexed_at": summary.get("indexed_at"),
            "corpus_enabled": bool(cfg.get("ingest_index_sources"))}


def _code(cfg: Config) -> dict[str, Any]:
    """Code corpus at a glance (C05). `age_hours` is the number that matters:
    with no watcher over repositories (D4) this corpus goes stale silently, and
    a stale code index answers with yesterday's implementation."""
    from .search import indexer

    if cfg.code_repos_dir is None:
        return {"present": False, "configured": False}
    summary = indexer.index_summary(cfg.code_index_dir)
    if summary is None:
        return {"present": False, "configured": True,
                "root": str(cfg.code_repos_dir)}
    files = indexer.index_files(cfg.code_index_dir)
    age_h = None
    if files is not None:
        age_h = round((time.time() - files[0].stat().st_mtime) / 3600, 1)
    return {"present": True, "configured": True, "root": str(cfg.code_repos_dir),
            "chunks": summary.get("chunk_count"), "files": summary.get("file_count"),
            "model": summary.get("model"), "age_hours": age_h}


def _cache_age(cfg: Config) -> dict[str, Any]:
    from . import nightly
    from . import reflect
    out: dict[str, Any] = {}
    for name, fname in (("digest", nightly.DIGEST_SNAPSHOT_NAME),
                        ("curator", nightly.CURATOR_SNAPSHOT_NAME),
                        ("config", nightly.CONFIG_SNAPSHOT_NAME),
                        ("reflect", reflect.SNAPSHOT_NAME)):
        p = cfg.mb_dir / fname
        present = p.exists()
        entry: dict[str, Any] = {"present": present}
        if present:
            entry["age_hours"] = round((time.time() - p.stat().st_mtime) / 3600, 1)
        out[name] = entry
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
    from .config import machine_id
    from . import hooks_state
    return {
        "vault": str(cfg.vault),
        "machine_id": machine_id(),
        "index": _guard(lambda: _index(cfg)),
        "daemon": _guard(lambda: client.status(cfg)),
        "watcher": _guard(lambda: _watcher(cfg)),
        "scheduler": _guard(lambda: _scheduler(cfg)),
        "disk": _guard(lambda: _disk(cfg)),
        "hooks": _guard(lambda: hooks_state.status(cfg.vault)),
        "backlog": _guard(lambda: _backlog(cfg)),
        "sources": _guard(lambda: _sources(cfg)),
        "code": _guard(lambda: _code(cfg)),
        "cache_age": _guard(lambda: _cache_age(cfg)),
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
    src = snap["sources"]
    if src.get("present"):
        print(f"sources: {src['documents']} documents "
              f"({src['unprocessed']} unprocessed), {src['chunks']} chunks"
              + ("" if src.get("corpus_enabled") else ", corpus DISABLED"))
    code = snap["code"]
    if code.get("configured"):
        if code.get("present"):
            print(f"code: {code['chunks']} chunks from {code['files']} files, "
                  f"age {code['age_hours']}h")
        else:
            print("code: configured but not built (`mb index rebuild --code`)")
    print(f"disk(.mb): {snap['disk'].get('mb_dir_mb')} MB")
    h = snap["hooks"]
    if "error" not in h:
        desired = "enabled" if h.get("configured") else "disabled"
        clients = ", ".join(f"{n}={'yes' if c['installed'] else 'no'}"
                            for n, c in h.get("clients", {}).items())
        print(f"capture hooks: {desired} (source: {h.get('source')}); installed: {clients}")
    return 0
