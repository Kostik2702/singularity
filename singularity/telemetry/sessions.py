"""Full-text search over your own past sessions — opt-in, local, disposable.

`mb search` answers from the vault: what somebody decided to write down. A great
deal never gets written down, and the question "where did we discuss
IndexLockedError" has had no answer at all — while the transcripts sat on disk
being walked by `mb reflect` every night, counted, and discarded.

**This is the one feature in the engine that stores someone else's text**, so
the contract is written before the code (`decisions/TRANSCRIPTS D1`):

* **Off by default.** `session_search_enabled` must be turned on deliberately.
  A function that keeps conversations is a choice, not a behaviour.
* **Its own store.** `.mb/sessions.db`, not the memory index. `mb search`
  without `--sessions` returns exactly what it returned before this existed —
  by construction, a different database, not by filtering.
* **Redacted on the way in.** The same sanitiser as `mb learn`. A transcript is
  the likeliest carrier of a secret in the whole system: it holds whatever was
  on screen, including the output of a command that printed a key.
* **Finite.** `session_search_retention_days` prunes on every store; `mb
  sessions purge` deletes everything. An opt-in that cannot be undone is not
  opt-in.

FTS5 rather than embeddings, deliberately: a question about a past session is
almost always "where did we talk about <this exact thing>", which is a lexical
query. Hermes reached the same conclusion for the same reason, and its
`session_search` also returns real messages rather than summaries — a summary
loses precisely the token you came looking for.

**Coverage is Claude transcripts only.** Gemini and Grok are discovered by
`reflect` for counting but their message shapes are not parsed here yet; saying
"sessions are searchable" while two of four clients are silently absent is the
kind of claim this project does not make. `stats()` reports which clients are
covered.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config

DB_NAME = "sessions.db"
COVERED_CLIENTS = ("claude",)
# Text shorter than this is a fragment ("ok", "yes, do that") that would flood
# the index without ever being the thing someone searches for.
MIN_TEXT_CHARS = 40
MAX_TEXT_CHARS = 4000


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("session_search_enabled"))


def db_path(cfg: Config) -> Path:
    return cfg.mb_dir / DB_NAME


def _connect(cfg: Config) -> sqlite3.Connection:
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(cfg))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS messages USING fts5("
        "  client, role, project, day UNINDEXED, ts UNINDEXED, body)")
    conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def _get_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO state(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def _claude_messages(path: Path) -> list[tuple[str, str, float]]:
    """(role, text, epoch) for the substantive turns of one transcript.

    Tool calls and their results are skipped: they are already reconstructable
    from the vault and the repository, and they are the bulk of a transcript's
    size. What is not reconstructable is what was said.
    """
    out: list[tuple[str, str, float]] = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("isSidechain") is True:
                    continue
                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                role = str(message.get("role") or "")
                if role not in ("user", "assistant"):
                    continue
                content = message.get("content")
                blocks = content if isinstance(content, list) else [
                    {"type": "text", "text": content}] if isinstance(content, str) else []
                texts = [b.get("text") for b in blocks
                         if isinstance(b, dict) and b.get("type") == "text"]
                body = "\n".join(t for t in texts if isinstance(t, str)).strip()
                if len(body) < MIN_TEXT_CHARS:
                    continue
                ts = obj.get("timestamp")
                epoch = _epoch(ts) or path.stat().st_mtime
                out.append((role, body[:MAX_TEXT_CHARS], epoch))
    except OSError:
        return []
    return out


def _epoch(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def store(cfg: Config, *, now: float | None = None) -> dict[str, Any]:
    """Index sessions written since the last run. No-op unless enabled."""
    if not enabled(cfg):
        return {"status": "disabled", "indexed": 0}

    from ..knowledge import redact
    from . import reflect as reflect_mod

    stamp = now if now is not None else time.time()
    conn = _connect(cfg)
    try:
        since = float(_get_state(conn, "scanned_through", "0") or 0)
        registry = cfg.get("projects", {}) or {}
        indexed = redacted = 0
        paths = reflect_mod._claude_session_paths(
            reflect_mod.claude_transcript_root(), since)
        for path in paths:
            project = _project_of(path, cfg, registry)
            for role, body, epoch in _claude_messages(path):
                scrubbed = redact.redact(body)
                if scrubbed.findings:
                    redacted += len(scrubbed.findings)
                day = datetime.fromtimestamp(epoch).date().isoformat()
                conn.execute(
                    "INSERT INTO messages(client, role, project, day, ts, body) "
                    "VALUES(?,?,?,?,?,?)",
                    ("claude", role, project, day, epoch, scrubbed.text))
                indexed += 1
        _set_state(conn, "scanned_through", str(stamp))
        pruned = _prune(conn, cfg, stamp)
        conn.commit()
        return {"status": "ok", "indexed": indexed, "redacted": redacted,
                "pruned": pruned, "sessions": len(paths)}
    finally:
        conn.close()


def _project_of(path: Path, cfg: Config, registry: dict) -> str:
    """Best-effort project for a transcript, from the first `cwd` it records."""
    from ..machine import projects as projects_mod
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(cwd, str) and cwd.strip():
                    return projects_mod.detect_project(registry, cwd) or "general"
    except OSError:
        pass
    return "general"


def _prune(conn: sqlite3.Connection, cfg: Config, now: float) -> int:
    days = float(cfg.get("session_search_retention_days") or 90)
    cutoff = now - days * 86400
    cur = conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
    return cur.rowcount or 0


def search(cfg: Config, query: str, *, limit: int = 5,
           project: str | None = None) -> list[dict[str, Any]]:
    """Matching turns, newest first. Empty when disabled or never stored."""
    if not enabled(cfg) or not db_path(cfg).exists() or not query.strip():
        return []
    conn = _connect(cfg)
    try:
        sql = ("SELECT role, project, day, body FROM messages "
               "WHERE messages MATCH ?")
        args: list[Any] = [_fts_query(query)]
        if project:
            sql += " AND project = ?"
            args.append(project)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(int(limit))
        try:
            rows = conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []      # a malformed FTS expression is a bad query, not a crash
        return [{"role": r[0], "project": r[1], "day": r[2], "body": r[3]} for r in rows]
    finally:
        conn.close()


def _fts_query(query: str) -> str:
    """Quote every term so punctuation in a natural question cannot be read as
    FTS5 syntax — `IndexLockedError` and `--per-signal` are exactly the queries
    this is for, and both would otherwise be operators."""
    terms = [t for t in query.replace('"', " ").split() if t]
    return " ".join(f'"{t}"' for t in terms) or '""'


def stats(cfg: Config) -> dict[str, Any]:
    path = db_path(cfg)
    out: dict[str, Any] = {"enabled": enabled(cfg), "present": path.exists(),
                           "covered_clients": list(COVERED_CLIENTS)}
    if not path.exists():
        return {**out, "messages": 0, "size_mb": 0.0}
    conn = _connect(cfg)
    try:
        rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        conn.close()
    return {**out, "messages": int(rows),
            "size_mb": round(path.stat().st_size / (1024 * 1024), 1)}


def purge(cfg: Config) -> dict[str, Any]:
    """Delete the store outright. Transcripts and the vault are untouched."""
    path = db_path(cfg)
    existed = path.exists()
    for suffix in ("", "-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return {"status": "purged" if existed else "nothing to purge"}
