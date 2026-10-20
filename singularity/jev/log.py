"""The Jev journal: one JSON line per call, the contract the mission-control
tab reads.

Path: `<machine_dir>/jev/log.jsonl` (git-ignored by the vault, machine-local).
Append-only, one `write` per line. Past 5 MiB the file moves to `log.1.jsonl`
(one generation), under a lock so two writers cannot both rotate. The
generation it replaces is gone for good, so its cost is first added to
`totals.json`: the all-time cost never resets, which is what the balance is
computed from. Writing is best-effort: a journal that cannot be written must
never cost the prompt or the search it describes.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from ..config import atomic_write_json
from ..knowledge.redact import redact
from ..platform.fslock import make_lock_dir

MAX_BYTES = 5 * 1024 * 1024
HEAD_CHARS = 80
STALE_LOCK_SECONDS = 60
SIZES = ("tiny", "small", "medium", "large")


def jev_dir(cfg) -> Path:
    return cfg.machine_dir / "jev"


def path(cfg) -> Path:
    return jev_dir(cfg) / "log.jsonl"


def _totals_path(cfg) -> Path:
    return jev_dir(cfg) / "totals.json"


def carried_cost(cfg) -> float:
    """Cost of every generation that has already been rotated away."""
    try:
        value = json.loads(_totals_path(cfg).read_text(encoding="utf-8")).get("cost", 0.0)
        return float(value)
    except (OSError, ValueError, AttributeError, TypeError):
        return 0.0


def _rows_of(file: Path) -> list[dict]:
    try:
        raw = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _rotate(cfg, target: Path) -> None:
    """Move `target` to `log.1.jsonl` if it is still over the limit.

    The size is checked again under the lock: two writers that both saw an
    oversized file would otherwise rotate twice, and the second would move the
    fresh file over the first generation."""
    lock = target.parent / "rotate.lock"
    try:
        if lock.exists() and time.time() - lock.stat().st_mtime > STALE_LOCK_SECONDS:
            shutil.rmtree(lock, ignore_errors=True)
        if not make_lock_dir(lock):
            return                        # someone else is rotating; append regardless
    except OSError:
        return
    try:
        if target.exists() and target.stat().st_size > MAX_BYTES:
            old = target.with_name("log.1.jsonl")
            lost = sum(float(r.get("cost") or 0.0) for r in _rows_of(old))
            if lost:
                atomic_write_json(_totals_path(cfg), {"cost": carried_cost(cfg) + lost})
            os.replace(target, old)
    except OSError:
        pass
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def head_of(text: str) -> str:
    """First characters of the text, secrets masked, newlines flattened."""
    return " ".join(redact(text or "").text.split())[:HEAD_CHARS]


def append(cfg, kind: str, *, text: str = "", session: str | None = None,
           size: str | None = None, followup: float | None = None,
           confidence: float | None = None, decided: str | None = None,
           route: str | None = None, cost: float = 0.0, ms: int = 0,
           model: str | None = None, error: str | None = None,
           request_id: str | None = None) -> bool:
    """True when the line landed. `error` is `"<kind>:<status>"`; cost is 0 then."""
    record = {
        "ts": datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "session": session or os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
        "kind": kind,
        "head": head_of(text),
        "chars": len(text or ""),
        "size": size,
        "followup": followup,
        "confidence": confidence,
        "decided": decided,
        "route": route,
        "cost": 0.0 if error else cost,
        "ms": ms,
        "model": model,
        "error": error,
        "request_id": request_id,
        "test": os.environ.get("MB_JEV_TEST") == "1",
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        if not cfg.vault.is_dir():
            return False
        target = path(cfg)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            if target.stat().st_size > MAX_BYTES:
                _rotate(cfg, target)
        except FileNotFoundError:
            pass
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True
    except OSError:
        return False


def read(cfg) -> list[dict]:
    """Every parseable line of both generations, oldest first."""
    target = path(cfg)
    return _rows_of(target.with_name("log.1.jsonl")) + _rows_of(target)


def stats(cfg, session: str | None = None) -> dict:
    rows = read(cfg)
    if session is not None:
        rows = [r for r in rows if r.get("session") == session]
    by_size = dict.fromkeys(SIZES, 0)
    cost_by_kind: dict[str, float] = {}
    for row in rows:
        if row.get("size") in by_size:
            by_size[row["size"]] += 1
        kind = str(row.get("kind"))
        cost_by_kind[kind] = cost_by_kind.get(kind, 0.0) + float(row.get("cost") or 0.0)
    return {
        "calls": len(rows),
        "by_size": by_size,
        "decided_self": sum(1 for r in rows if r.get("decided") == "self"),
        "errors": sum(1 for r in rows if r.get("error")),
        "cost_total": sum(cost_by_kind.values()) + (carried_cost(cfg) if session is None else 0.0),
        "cost_by_kind": cost_by_kind,
        "last_call": rows[-1].get("ts") if rows else None,
    }
