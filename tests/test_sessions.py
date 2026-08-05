"""Gate for the opt-in store of past sessions.

This is the only feature in the engine that keeps someone else's text, so the
tests are mostly about the promises in `decisions/TRANSCRIPTS D1`, not about
search quality: off by default, its own store, redacted on the way in, finite,
and deletable. A feature that quietly failed any one of those would still look
like it worked.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.config import Config  # noqa: E402
from singularity.telemetry import sessions  # noqa: E402

# Anchored to real time on purpose: a transcript with no `timestamp` field
# falls back to its file mtime, so a NOW far in the future would put every
# row past retention the instant it was written — which is exactly what a
# fixed epoch constant did on the first draft of these tests.
NOW = time.time()


def _transcript(tmp_path: Path, *bodies: str, role: str = "user") -> Path:
    path = tmp_path / "t.jsonl"
    lines = []
    for body in bodies:
        lines.append(json.dumps({
            "cwd": str(tmp_path),
            "message": {"role": role, "content": [{"type": "text", "text": body}]},
        }))
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return Config(vault=tmp_path, data={"session_search_enabled": True})


@pytest.fixture
def wire(monkeypatch, tmp_path):
    def _wire(path: Path):
        monkeypatch.setattr(
            "singularity.telemetry.reflect._claude_session_paths",
            lambda root, since: [path])
        monkeypatch.setattr(
            "singularity.telemetry.reflect.claude_transcript_root",
            lambda: tmp_path)
    return _wire


# --- the promises ----------------------------------------------------------

def test_off_by_default():
    """A feature that stores conversations must be a choice, not a behaviour."""
    from singularity.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["session_search_enabled"] is False


def test_disabled_stores_nothing_and_searches_nothing(tmp_path, wire):
    cfg = Config(vault=tmp_path, data={})
    wire(_transcript(tmp_path, "a" * 60))
    assert sessions.store(cfg, now=NOW)["status"] == "disabled"
    assert not sessions.db_path(cfg).exists()
    assert sessions.search(cfg, "aaa") == []


def test_secrets_are_masked_before_the_row_is_written(cfg, wire):
    """A transcript holds whatever was on screen — including the output of a
    command that printed a key. This is the likeliest carrier in the system."""
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    wire(_transcript(tmp_path_of(cfg), f"the deploy failed until we set KEY={secret} in env"))
    result = sessions.store(cfg, now=NOW)
    assert result["redacted"] >= 1
    hits = sessions.search(cfg, "deploy failed")
    assert hits and secret not in hits[0]["body"]
    assert "sk-XXXXXXXX" in hits[0]["body"]
    raw = sessions.db_path(cfg).read_bytes()
    assert secret.encode() not in raw, "the raw key reached the database file"


def test_purge_removes_the_store(cfg, wire):
    wire(_transcript(tmp_path_of(cfg), "something worth keeping for a while"))
    sessions.store(cfg, now=NOW)
    assert sessions.db_path(cfg).exists()
    assert sessions.purge(cfg)["status"] == "purged"
    assert not sessions.db_path(cfg).exists()
    assert sessions.search(cfg, "something") == []


def test_retention_prunes_old_rows(cfg, wire):
    wire(_transcript(tmp_path_of(cfg), "an old conversation about the daemon reload"))
    sessions.store(cfg, now=NOW)
    assert sessions.stats(cfg)["messages"] == 1
    cfg.data["session_search_retention_days"] = 1
    # a later run prunes what has aged past the window
    sessions.store(cfg, now=NOW + 5 * 86400)
    assert sessions.stats(cfg)["messages"] == 0


# --- what it actually does -------------------------------------------------

def test_finds_an_exact_identifier(cfg, wire):
    """The query this exists for. FTS5 over punctuation-quoted terms, because
    `IndexLockedError` and `--per-signal` would otherwise be FTS operators."""
    wire(_transcript(tmp_path_of(cfg),
                     "we chased IndexLockedError for an hour before finding the lock",
                     "unrelated talk about typography and spacing in the header"))
    sessions.store(cfg, now=NOW)
    hits = sessions.search(cfg, "IndexLockedError")
    assert len(hits) == 1 and "IndexLockedError" in hits[0]["body"]


def test_a_punctuation_heavy_query_does_not_crash(cfg, wire):
    wire(_transcript(tmp_path_of(cfg), "the flag --per-signal caps items per signal"))
    sessions.store(cfg, now=NOW)
    assert sessions.search(cfg, "--per-signal")
    sessions.search(cfg, 'a "b OR NEAR(')            # must not raise


def test_short_fragments_are_not_indexed(cfg, wire):
    """"ok", "yes do that" would flood the store without ever being searched."""
    wire(_transcript(tmp_path_of(cfg), "ok", "yes", "x" * 80))
    assert sessions.store(cfg, now=NOW)["indexed"] == 1


def test_the_next_run_starts_where_the_last_one_stopped(cfg, wire, monkeypatch):
    path = _transcript(tmp_path_of(cfg), "a first substantive message about indexing")
    wire(path)
    seen: list[float] = []

    def _paths(root, since):
        seen.append(since)
        return [path]

    monkeypatch.setattr("singularity.telemetry.reflect._claude_session_paths", _paths)
    sessions.store(cfg, now=NOW)
    sessions.store(cfg, now=NOW + 100)
    assert seen == [0.0, NOW]


def test_stats_says_which_clients_are_covered(cfg):
    snap = sessions.stats(cfg)
    assert snap["covered_clients"] == ["claude"], (
        "coverage has to be stated: claiming 'sessions are searchable' while "
        "two of four clients are absent is the claim this project does not make")


def tmp_path_of(cfg: Config) -> Path:
    return cfg.vault
