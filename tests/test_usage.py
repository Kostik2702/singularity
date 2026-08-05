"""Gate for note-open counting.

The point of this signal is to refine an existing one. `mb curator` can say a
note is old; age is a weak proxy for value in a knowledge vault, where a
decision from two years ago may be permanently correct. Old AND never once
opened is a much smaller, actionable set.

The property that matters most here is that counts ACCUMULATE. Transcripts are
pruned by their clients, so a collector that recomputed from scratch would
silently reset a note's history the moment its session aged out — and the number
would keep looking plausible.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.config import Config  # noqa: E402
from singularity.telemetry import usage  # noqa: E402
from singularity.telemetry.reflect import ReflectSession  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return Config(vault=tmp_path, data={})


def _sessions(*maps):
    out = []
    for m in maps:
        s = ReflectSession(client="claude", session_id="x")
        s.note_reads = dict(m)
        out.append(s)
    return out


def test_counts_are_written_and_read_back(cfg, monkeypatch):
    monkeypatch.setattr("singularity.telemetry.reflect.walk_sessions",
                        lambda c, since: _sessions({"rules/A.md": 2, "howto/B.md": 1}))
    summary = usage.update(cfg, now=1_000_000.0)
    assert summary == {"sessions": 1, "new_reads": 3, "notes_tracked": 2}
    assert usage.reads_by_note(cfg) == {"rules/A.md": 2, "howto/B.md": 1}


def test_counts_accumulate_across_runs(cfg, monkeypatch):
    """The invariant the whole design turns on. A recomputing collector would
    return 1 on the second run and look entirely reasonable doing it."""
    monkeypatch.setattr("singularity.telemetry.reflect.walk_sessions",
                        lambda c, since: _sessions({"rules/A.md": 2}))
    usage.update(cfg, now=1_000_000.0)
    monkeypatch.setattr("singularity.telemetry.reflect.walk_sessions",
                        lambda c, since: _sessions({"rules/A.md": 1}))
    usage.update(cfg, now=1_000_100.0)
    assert usage.reads_by_note(cfg)["rules/A.md"] == 3


def test_the_next_walk_starts_where_the_last_one_stopped(cfg, monkeypatch):
    """Without this the same session is counted on every nightly run, and a
    note's count grows for as long as its transcript survives."""
    seen: list[float] = []

    def _walk(c, since):
        seen.append(since)
        return _sessions({"rules/A.md": 1})

    monkeypatch.setattr("singularity.telemetry.reflect.walk_sessions", _walk)
    usage.update(cfg, now=1_000_000.0)
    usage.update(cfg, now=1_000_500.0)
    assert seen == [0.0, 1_000_000.0]


def test_a_corrupt_file_degrades_to_nothing_known(cfg):
    """This is read on the search path's construction. An unreadable counter
    file must mean "no tie-break information", never an exception."""
    usage.path(cfg).write_text("{ not json", encoding="utf-8")
    assert usage.reads_by_note(cfg) == {}
    assert usage.load(cfg)["notes"] == {}


def test_a_file_of_the_wrong_shape_is_ignored(cfg):
    usage.path(cfg).write_text(json.dumps({"notes": ["not", "a", "dict"]}), encoding="utf-8")
    assert usage.reads_by_note(cfg) == {}


def test_absent_file_is_not_an_error(cfg):
    assert usage.reads_by_note(cfg) == {}


def test_never_opened_answers_against_the_notes_it_was_given(cfg, monkeypatch):
    monkeypatch.setattr("singularity.telemetry.reflect.walk_sessions",
                        lambda c, since: _sessions({"rules/A.md": 1}))
    usage.update(cfg, now=1_000_000.0)
    assert usage.never_opened(cfg, ["rules/A.md", "howto/B.md"]) == ["howto/B.md"]


def test_only_markdown_notes_are_counted():
    """A read of `.mb/config.json` is machinery, not knowledge — counting it
    would put the engine's own files at the top of the most-useful list."""
    from singularity.telemetry.reflect import _note_opened

    s = ReflectSession(client="claude", session_id="x")
    vault = Path("/v")
    _note_opened(s, vault / "rules" / "A.md", vault)
    _note_opened(s, vault / ".mb" / "config.json", vault)
    _note_opened(s, Path("/elsewhere/other.md"), vault)
    assert s.note_reads == {"rules/A.md": 1}


def test_note_reads_do_not_reach_the_reflect_snapshot(cfg, monkeypatch):
    """`reflect-last.json` is counts-and-rates by contract. Paths belong in
    usage.json, which says so; leaking them here would change what a file
    documented as content-free contains."""
    from singularity.telemetry import reflect as reflect_mod

    monkeypatch.setattr(reflect_mod, "_walk",
                        lambda c, since: (_sessions({"rules/A.md": 1}), 0, 0))
    snapshot = reflect_mod.reflect(cfg, days=30)
    blob = json.dumps(snapshot, ensure_ascii=False)
    assert "rules/A.md" not in blob
    assert "note_reads" not in blob
