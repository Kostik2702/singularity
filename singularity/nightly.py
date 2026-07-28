"""Nightly orchestration (T12): index update -> digest/curator/config
snapshot -> trend line -> markers sweep.

`mb nightly` replaces the standalone "nightly-index" job (T02, `index
update` only) -- see NIGHTLY_JOB/migrate_scheduler_job, the single source
both `mb doctor --fix` and the installer use so the two registration paths
    can't drift. The index step calls the warm daemon, which reuses
    indexer.update()'s writer lock (the same lock the watcher already goes
    through); the other steps only ever read
the committed generation or touch their own snapshot files, so they need no
lock. Every step is independently guarded (one failing step never aborts the
chain) and every persisted file is a tmp+replace atomic write.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config, atomic_write_json

DIGEST_SNAPSHOT_NAME = "digest-last.json"
CURATOR_SNAPSHOT_NAME = "curator-last.json"
CONFIG_SNAPSHOT_NAME = "config-last.json"
GAPS_SNAPSHOT_NAME = "gaps-last.json"                     # W11
CONTRADICTIONS_SNAPSHOT_NAME = "contradictions-last.json"  # W11
TREND_HISTORY_NAME = "nightly-history.jsonl"

JOB_NAME = "nightly"
LEGACY_JOB_NAME = "nightly-index"   # T02 job this one replaces (T12 migration)


def nightly_job(cfg: Config):
    from .platform.scheduler import Job

    return Job(JOB_NAME,
               ["-m", "singularity", "--vault", str(cfg.vault), "nightly"],
               daily_at=str(cfg.get("nightly_schedule_time")))


def migrate_scheduler_job(cfg: Config) -> str:
    """Idempotently replace the legacy standalone "nightly-index" job (which
    only ran `index update`) with the "nightly" job (whose first step IS an
    index update, plus digest/curator/config/trend/markers). Single source
    for `mb doctor --fix` and the installer so they can't drift apart."""
    from .platform import scheduler

    legacy = scheduler.Job(LEGACY_JOB_NAME, [], daily_at="03:00")
    if scheduler.verify(legacy).get("registered"):
        scheduler.unregister(legacy)
    return scheduler.register(nightly_job(cfg))


def _guard(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — one failing nightly step must not sink the rest
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# steps
# ---------------------------------------------------------------------------

def _step_index(cfg: Config) -> dict[str, Any]:
    from .search import client

    try:
        stats = client.run_index(cfg, "update")
    except client.RemoteIndexError as exc:
        if exc.error_type == "IndexLockedError":
            return {"status": "skipped", "reason": "locked"}
        raise
    return {"status": "ok", "stats": stats}


def _step_digest(cfg: Config, now: str) -> dict[str, Any]:
    from . import digest

    result = digest.plan(cfg)
    atomic_write_json(cfg.mb_dir / DIGEST_SNAPSHOT_NAME, {"generated_at": now, **result})
    return {"status": "ok", "entries": len(result["entries"]),
            "index_status": result["index_status"]}


def _step_curator(cfg: Config, now: str) -> dict[str, Any]:
    from . import curator

    issues = curator.validate(cfg)
    report = curator.analyze(cfg)
    lint_report = curator.lint(cfg)   # T13: chunk-level lint, part of the same snapshot
    atomic_write_json(cfg.mb_dir / CURATOR_SNAPSHOT_NAME,
                       {"generated_at": now, "issues": issues, "lint": lint_report, **report})
    return {"status": "ok", "issues": sum(len(v) for v in issues.values()),
            "notes": report["notes"],
            "duplicates": len(lint_report["duplicates"]["same_file"])
                         + len(lint_report["duplicates"]["cross_file"]),
            "supersession": len(lint_report["supersession"])}


def _step_config(cfg: Config, now: str) -> dict[str, Any]:
    from .config import DEFAULT_CONFIG, validate_config

    merged = {**DEFAULT_CONFIG, **cfg.data}
    result = validate_config(merged)
    atomic_write_json(cfg.mb_dir / CONFIG_SNAPSHOT_NAME, {
        "generated_at": now, "schema_version": merged.get("schema_version"),
        "errors": result["errors"], "warnings": result["warnings"],
    })
    return {"status": "ok", "errors": len(result["errors"]),
            "warnings": len(result["warnings"])}


def _step_ingest_index(cfg: Config) -> dict[str, Any]:
    """Catch the sources corpus up. It needs its own step because the watcher
    does NOT see `.mb/` (a direct consequence of decision D1) — without this,
    a document ingested while the daemon was cold stays unsearchable until
    someone runs `mb ingest index` by hand."""
    from . import sources as sources_mod
    from .search import client

    if not cfg.get("ingest_index_sources"):
        return {"status": "skipped", "reason": "ingest_index_sources=false"}
    if not any(rec.get("stored") for rec in
               sources_mod.read_registry(cfg)["sources"].values()
               if isinstance(rec, dict)):
        return {"status": "skipped", "reason": "no sources"}
    try:
        stats = client.run_index(cfg, "update", corpus="sources")
    except client.RemoteIndexError as exc:
        if exc.error_type == "IndexLockedError":
            return {"status": "skipped", "reason": "locked"}
        raise
    return {"status": "ok", "stats": stats}


def _step_gaps(cfg: Config, now: str) -> dict[str, Any]:
    from . import gaps

    result = gaps.report(cfg)
    atomic_write_json(cfg.mb_dir / GAPS_SNAPSHOT_NAME, {"generated_at": now, **result})
    return {"status": "ok", "total": result["total"], **result["counts"]}


def _step_contradictions(cfg: Config, now: str) -> dict[str, Any]:
    from . import contradictions

    report = contradictions.scan(cfg)
    atomic_write_json(cfg.mb_dir / CONTRADICTIONS_SNAPSHOT_NAME,
                      {"generated_at": now, **report})
    return {"status": "ok", "candidates": report["total"],
            "suppressed": report["suppressed"], "capped": report["capped"]}


def _step_ingest_log(cfg: Config) -> dict[str, Any]:
    from . import sources as sources_mod

    return {"status": "ok", **sources_mod.sweep_log(cfg)}


def _step_sources(cfg: Config) -> dict[str, Any]:
    from . import sources as sources_mod

    records = [r for r in sources_mod.read_registry(cfg)["sources"].values()
               if isinstance(r, dict)]
    by_status: dict[str, int] = {}
    for record in records:
        key = str(record.get("status", "?"))
        by_status[key] = by_status.get(key, 0) + 1
    return {"status": "ok", "total": len(records), "by_status": by_status}


def _step_markers(cfg: Config) -> dict[str, Any]:
    markers_dir = cfg.markers_dir
    if not markers_dir.exists():
        return {"status": "ok", "swept": 0}
    retention_days = float(cfg.get("markers_retention_days"))
    cutoff = time.time() - retention_days * 86400
    swept = 0
    for p in markers_dir.iterdir():
        if not p.is_file():
            continue
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                swept += 1
        except OSError:
            continue
    return {"status": "ok", "swept": swept}


def _step_trend(cfg: Config, now: str, steps: dict[str, Any]) -> dict[str, Any]:
    """Append one row per run; prune rows older than the retention window on
    every write so the history file never grows unbounded."""
    path = cfg.mb_dir / TREND_HISTORY_NAME
    gaps_step = steps.get("gaps", {})
    row = {
        "ts": now,
        "index_status": steps.get("digest", {}).get("index_status"),
        "digest_entries": steps.get("digest", {}).get("entries"),
        "curator_issues": steps.get("curator", {}).get("issues"),
        "curator_notes": steps.get("curator", {}).get("notes"),
        "config_errors": steps.get("config", {}).get("errors"),
        "markers_swept": steps.get("markers", {}).get("swept"),
        # W11: the ingest cycle's own trend line
        "sources": steps.get("sources", {}).get("total"),
        "unprocessed_sources": gaps_step.get("unprocessed_sources"),
        "open_questions": gaps_step.get("open_questions"),
        "contradiction_candidates": steps.get("contradictions", {}).get("candidates"),
    }

    old_lines: list[str] = []
    if path.exists():
        try:
            old_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            old_lines = []

    retention_days = float(cfg.get("nightly_trend_retention_days"))
    cutoff = time.time() - retention_days * 86400
    kept: list[str] = []
    for line in old_lines:
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"]).timestamp()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue   # drop unparseable rows rather than let them accumulate forever
        if ts >= cutoff:
            kept.append(line)
    kept.append(json.dumps(row, ensure_ascii=False))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(path)
    return {"status": "ok", "rows": len(kept)}


# ---------------------------------------------------------------------------

def run(cfg: Config) -> dict[str, Any]:
    now = datetime.now().isoformat(timespec="seconds")
    steps: dict[str, Any] = {}
    steps["index"] = _guard(lambda: _step_index(cfg))
    steps["ingest_index"] = _guard(lambda: _step_ingest_index(cfg))
    steps["digest"] = _guard(lambda: _step_digest(cfg, now))
    steps["curator"] = _guard(lambda: _step_curator(cfg, now))
    steps["gaps"] = _guard(lambda: _step_gaps(cfg, now))
    steps["contradictions"] = _guard(lambda: _step_contradictions(cfg, now))
    steps["sources"] = _guard(lambda: _step_sources(cfg))
    steps["config"] = _guard(lambda: _step_config(cfg, now))
    steps["markers"] = _guard(lambda: _step_markers(cfg))
    steps["ingest_log"] = _guard(lambda: _step_ingest_log(cfg))
    steps["trend"] = _guard(lambda: _step_trend(cfg, now, steps))
    return {"started_at": now, "steps": steps}


def has_error(result: dict[str, Any]) -> bool:
    return any(s.get("status") == "error" for s in result.get("steps", {}).values())
