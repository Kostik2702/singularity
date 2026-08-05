"""Gate for `mb journey`.

The question "what did we work out last month" had no answer that did not
involve opening five folders by hand. Two properties decide whether this
command answers it or merely looks like it does:

* a note counts on the day it was CREATED, not the day it was touched — mtime
  would float every edited file to the top and bury the six real decisions;
* a learnings file counts once per timestamped section, not once per file —
  those files are appended to for months, so the file's own date says nothing.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.config import Config  # noqa: E402
from singularity.knowledge import journey  # noqa: E402
from singularity.render import journey as render_journey  # noqa: E402

TODAY = date(2026, 8, 5)


def _note(vault: Path, rel: str, *, created: str, ntype: str, title: str,
          project: str | None = None, body: str = "") -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    proj = f"project: {project}\n" if project else ""
    path.write_text(f'---\ntype: {ntype}\ntitle: "{title}"\ncreated: {created}\n{proj}---\n\n'
                    f"# {title}\n{body}\n", encoding="utf-8")


@pytest.fixture
def cfg(tmp_path):
    _note(tmp_path, "decisions/Recent.md", created="2026-08-04",
          ntype="decision", title="Recent decision")
    _note(tmp_path, "rules/Old.md", created="2026-01-01",
          ntype="rule", title="Ancient rule")
    _note(tmp_path, "howto/Scoped.md", created="2026-08-03",
          ntype="howto", title="Scoped howto", project="singularity")
    _note(tmp_path, "daily/2026-08-04.md", created="2026-08-04",
          ntype="daily", title="A daily page")
    return Config(vault=tmp_path, data={})


def test_only_the_window_is_reported(cfg):
    out = journey.collect(cfg, days=7, today=TODAY)
    titles = [e["title"] for e in out["entries"]]
    assert "Recent decision" in titles
    assert "Ancient rule" not in titles, "a note outside the window was reported"
    assert out["from"] == "2026-07-30" and out["to"] == "2026-08-05"


def test_journal_folders_are_not_knowledge(cfg):
    """Sixty dailies would bury the six decisions that are the point of asking."""
    out = journey.collect(cfg, days=7, today=TODAY)
    assert "A daily page" not in [e["title"] for e in out["entries"]]


def test_creation_date_decides_not_mtime(cfg):
    """Touching an old note must not make it this month's knowledge."""
    old = cfg.vault / "rules" / "Old.md"
    old.write_text(old.read_text(encoding="utf-8") + "\nedited today\n", encoding="utf-8")
    out = journey.collect(cfg, days=7, today=TODAY)
    assert "Ancient rule" not in [e["title"] for e in out["entries"]]


def test_project_scope_filters(cfg):
    out = journey.collect(cfg, days=7, project="singularity", today=TODAY)
    titles = [e["title"] for e in out["entries"]]
    assert titles == ["Scoped howto"]


def test_entries_are_newest_first(cfg):
    out = journey.collect(cfg, days=30, today=TODAY)
    days = [e["day"] for e in out["entries"]]
    assert days == sorted(days, reverse=True)


# --- learning entries come from sections, not files ------------------------

def _index(cfg: Config, chunks: list[dict]) -> None:
    """Write a committed index the collector can read, without an encoder."""
    gen = cfg.memory_index_dir / "gen_1"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "metadata.json").write_text(json.dumps({"chunks": chunks}), encoding="utf-8")
    (gen / "embeddings.npy").write_bytes(b"")
    (cfg.memory_index_dir / "CURRENT").write_text("gen_1", encoding="utf-8")


def test_a_learnings_file_yields_one_entry_per_dated_section(cfg, monkeypatch):
    monkeypatch.setattr(journey, "_notes", lambda *a, **k: [])
    _index(cfg, [
        {"type": "learning", "file": "learnings/active/T.md", "title": "T",
         "section": "2026-08-04 10:00 — first insight"},
        {"type": "learning", "file": "learnings/active/T.md", "title": "T",
         "section": "2026-08-05 11:00 — second insight"},
        {"type": "learning", "file": "learnings/active/T.md", "title": "T",
         "section": "2026-01-01 09:00 — ancient insight"},
    ])
    out = journey.collect(cfg, days=7, today=TODAY)
    labels = [e["label"] for e in out["entries"]]
    assert labels == ["second insight", "first insight"], (
        "sections must be the unit — one entry per file would report this "
        "months-old file as a single thing, or not at all")
    assert out["learning_tasks"] == 1


def test_a_long_section_split_into_chunks_counts_once(cfg, monkeypatch):
    """Chunking is an index detail. Counting chunks would make a verbose
    insight look like several."""
    monkeypatch.setattr(journey, "_notes", lambda *a, **k: [])
    _index(cfg, [
        {"type": "learning", "file": "learnings/active/T.md", "title": "T",
         "section": "2026-08-04 10:00 — one insight", "chunk_index": 0},
        {"type": "learning", "file": "learnings/active/T.md", "title": "T",
         "section": "2026-08-04 10:00 — one insight", "chunk_index": 1},
    ])
    out = journey.collect(cfg, days=7, today=TODAY)
    assert out["total"] == 1


def test_a_section_that_is_not_a_date_is_ignored(cfg, monkeypatch):
    monkeypatch.setattr(journey, "_notes", lambda *a, **k: [])
    _index(cfg, [
        {"type": "learning", "file": "learnings/active/T.md", "title": "T",
         "section": "Обзор подхода"},
    ])
    assert journey.collect(cfg, days=7, today=TODAY)["total"] == 0


def test_a_missing_index_is_not_an_error(cfg):
    out = journey.collect(cfg, days=7, today=TODAY)
    assert out["total"] >= 1          # notes still reported
    assert all(e["kind"] != "learning" for e in out["entries"])


# --- rendering --------------------------------------------------------------

def test_render_groups_by_day_and_reports_the_full_count(cfg):
    out = journey.collect(cfg, days=30, today=TODAY)
    text = render_journey(out, limit=1)
    assert "2026-08-04" in text
    assert f"{out['total']} entries" in text, "the summary must cover the window, not the page"
    assert "more (raise --limit" in text


def test_render_says_so_when_nothing_happened(cfg):
    out = journey.collect(cfg, days=1, today=date(2026, 12, 31))
    assert "(nothing in this window)" in render_journey(out)
