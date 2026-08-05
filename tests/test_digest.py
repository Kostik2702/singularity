"""T11: digest planner + journal.

Report-only planner over the COMMITTED index generation (no model/encoder) --
learnings/active sections -> reference/rule/howto/decision merge candidates,
plus an atomic mark journal so processed identities never resurface.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from singularity.knowledge import digest
from singularity.config import Config
from singularity.search import indexer


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _chunk(file, section, type_=None, project=None):
    c = {"file": file, "section": section, "title": section}
    if type_ is not None:
        c["type"] = type_
    if project is not None:
        c["project"] = project
    return c


def _commit(cfg, pairs):
    """pairs: [(chunk_dict, vector), ...] -> commits a whole generation."""
    chunks = [c for c, _ in pairs]
    embs = np.array([v for _, v in pairs], dtype=np.float32)
    indexer._commit_generation(
        cfg.memory_index_dir, embs,
        {"model": "m", "file_meta": {}, "chunks": chunks},
        chunk_schema=indexer.chunking.CHUNK_SCHEMA_VERSION)


# --- index status handling ---------------------------------------------------

def test_missing_index_reports_status_missing(cfg):
    result = digest.plan(cfg)
    assert result == {"entries": [], "index_status": "missing",
                      "source_count": 0, "target_count": 0}


def test_torn_index_reports_status_torn(cfg):
    # embeddings row count != chunk count -- write the mismatched pair directly
    # (bypassing _commit_generation's own torn guard) to simulate real corruption.
    d = cfg.memory_index_dir
    d.mkdir(parents=True)
    gen = d / "gen_torn"
    gen.mkdir()
    np.save(gen / "embeddings.npy", np.zeros((3, 2), dtype=np.float32), allow_pickle=False)
    (gen / "metadata.json").write_text(
        json.dumps({"chunks": [{"file": "a"}]}), encoding="utf-8")
    (d / "CURRENT").write_text("gen_torn", encoding="utf-8")
    result = digest.plan(cfg)
    assert result["index_status"] == "torn"
    assert result["entries"] == []


def test_legacy_flat_layout_still_plans(cfg):
    d = cfg.memory_index_dir
    d.mkdir(parents=True)
    chunks = [_chunk("learnings/active/T.md", "S1", "learning"),
              _chunk("reference/R.md", "R1", "reference")]
    embs = np.array([[1, 0], [1, 0]], dtype=np.float32)
    np.save(d / "embeddings.npy", embs, allow_pickle=False)
    (d / "metadata.json").write_text(
        json.dumps({"chunks": chunks}), encoding="utf-8")
    result = digest.plan(cfg, project_filter=None)
    assert result["index_status"] == "ok"
    assert result["source_count"] == 1
    assert result["target_count"] == 1
    assert result["entries"][0]["candidates"][0]["file"] == "reference/R.md"


# --- zero candidates ----------------------------------------------------------

def test_empty_vault_yields_no_entries(cfg):
    _commit(cfg, [(_chunk("reference/R.md", "R1", "reference"), [1, 0])])
    result = digest.plan(cfg)
    assert result["entries"] == []
    assert result["source_count"] == 0
    assert result["target_count"] == 1


def test_source_with_no_target_above_threshold_still_listed_empty(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "S1", "learning"), [1, 0]),
        (_chunk("reference/R.md", "R1", "reference"), [0, 1]),   # orthogonal, score 0
    ])
    result = digest.plan(cfg)
    assert len(result["entries"]) == 1
    entry = result["entries"][0]
    assert entry["file"] == "learnings/active/T.md" and entry["section"] == "S1"
    assert entry["candidates"] == []


# --- project isolation / general fallback --------------------------------------

def test_project_isolation_excludes_mismatched_project_target(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "B", "learning", project="proj1"), [0, 1]),
        (_chunk("howto/other.md", "H1", "howto", project="proj2"), [0, 1]),  # identical vector, wrong project
    ])
    result = digest.plan(cfg)
    entry = next(e for e in result["entries"] if e["section"] == "B")
    assert entry["candidates"] == []


def test_general_fallback_matches_any_source_project(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "B", "learning", project="proj1"), [0, 1]),
        (_chunk("reference/gen.md", "R1", "reference"), [0, 1]),   # no project -> general
    ])
    result = digest.plan(cfg)
    entry = next(e for e in result["entries"] if e["section"] == "B")
    assert [c["file"] for c in entry["candidates"]] == ["reference/gen.md"]


def test_general_source_does_not_match_project_scoped_target(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "A", "learning"), [1, 0]),           # general source
        (_chunk("rules/proj1.md", "R1", "rule", project="proj1"), [1, 0]),    # project-only target
    ])
    result = digest.plan(cfg)
    entry = next(e for e in result["entries"] if e["section"] == "A")
    assert entry["candidates"] == []


def test_personal_source_excluded_entirely(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "P", "learning", project="personal"), [1, 0]),
        (_chunk("reference/gen.md", "R1", "reference"), [1, 0]),
    ])
    result = digest.plan(cfg)
    assert result["source_count"] == 0
    assert result["entries"] == []


def test_personal_target_never_offered(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "A", "learning"), [1, 0]),
        (_chunk("reference/priv.md", "R1", "reference", project="personal"), [1, 0]),
    ])
    result = digest.plan(cfg)
    assert result["target_count"] == 0
    assert result["entries"][0]["candidates"] == []


def test_mistyped_learning_file_never_becomes_a_target(cfg):
    # a learnings/active file whose frontmatter mistakenly overrides
    # type: reference must still never qualify as a merge TARGET -- learnings
    # are structurally the input side of digestion, never the output side.
    _commit(cfg, [
        (_chunk("learnings/active/A.md", "A", "learning"), [1, 0]),
        (_chunk("learnings/active/Mistyped.md", "M", "reference"), [1, 0]),
        (_chunk("reference/Real.md", "R1", "reference"), [1, 0]),
    ])
    result = digest.plan(cfg)
    assert result["target_count"] == 1
    entry = next(e for e in result["entries"] if e["section"] == "A")
    assert [c["file"] for c in entry["candidates"]] == ["reference/Real.md"]


def test_project_filter_restricts_sources(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/A.md", "A", "learning"), [1, 0]),
        (_chunk("learnings/active/B.md", "B", "learning", project="proj1"), [1, 0]),
        (_chunk("reference/gen.md", "R1", "reference"), [1, 0]),
    ])
    result = digest.plan(cfg, project_filter={"proj1"})
    assert [e["section"] for e in result["entries"]] == ["B"]


# --- threshold / top-k ---------------------------------------------------------

def test_threshold_filters_low_score_candidates(cfg):
    cfg.data["digest_score_threshold"] = 0.9
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "S", "learning"), [1, 0]),
        (_chunk("reference/close.md", "R1", "reference"), [0.99, np.sqrt(1 - 0.99 ** 2)]),
        (_chunk("reference/far.md", "R2", "reference"), [0.5, np.sqrt(1 - 0.5 ** 2)]),
    ])
    result = digest.plan(cfg)
    files = [c["file"] for c in result["entries"][0]["candidates"]]
    assert files == ["reference/close.md"]


def test_top_k_caps_candidate_count_and_orders_by_score_desc(cfg):
    cfg.data["digest_top_k"] = 2
    cfg.data["digest_score_threshold"] = 0.0
    pairs = [(_chunk("learnings/active/T.md", "S", "learning"), [1, 0])]
    # three targets at descending similarity to [1, 0]
    for name, angle_cos in (("a.md", 1.0), ("b.md", 0.9), ("c.md", 0.5)):
        pairs.append((_chunk(f"reference/{name}", "R", "reference"),
                      [angle_cos, np.sqrt(max(0.0, 1 - angle_cos ** 2))]))
    _commit(cfg, pairs)
    result = digest.plan(cfg)
    cands = result["entries"][0]["candidates"]
    assert [c["file"] for c in cands] == ["reference/a.md", "reference/b.md"]
    assert cands[0]["score"] >= cands[1]["score"]


# --- marking --------------------------------------------------------------------

def test_marked_entries_disappear_from_plan(cfg):
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "S1", "learning"), [1, 0]),
        (_chunk("reference/R.md", "R1", "reference"), [1, 0]),
    ])
    result = digest.plan(cfg)
    assert len(result["entries"]) == 1
    digest.mark(cfg, [("learnings/active/T.md", "S1")])
    result2 = digest.plan(cfg)
    assert result2["entries"] == []


def test_mark_is_idempotent(cfg):
    _commit(cfg, [(_chunk("learnings/active/T.md", "S1", "learning"), [1, 0])])
    digest.mark(cfg, [("learnings/active/T.md", "S1")])
    digest.mark(cfg, [("learnings/active/T.md", "S1")])
    data = json.loads((cfg.mb_dir / "digest.json").read_text(encoding="utf-8"))
    assert list(data["marked"]) == ["learnings/active/T.md#S1"]


def test_mark_tolerant_of_renamed_deleted_source(cfg):
    # identity that never existed in any index -- marking must not raise
    count = digest.mark(cfg, [("learnings/active/gone.md", "Old Section")])
    assert count == 1
    data = json.loads((cfg.mb_dir / "digest.json").read_text(encoding="utf-8"))
    assert "learnings/active/gone.md#Old Section" in data["marked"]


def test_mark_writes_atomically_no_leftover_tmp(cfg):
    digest.mark(cfg, [("a.md", "S")])
    leftovers = [f for f in cfg.mb_dir.iterdir() if f.name.startswith("digest") and f.suffix == ".tmp"]
    assert leftovers == []


def test_journal_tolerates_malformed_file(cfg):
    cfg.mb_dir.mkdir(parents=True)
    (cfg.mb_dir / "digest.json").write_text("{not json", encoding="utf-8")
    _commit(cfg, [(_chunk("learnings/active/T.md", "S1", "learning"), [1, 0])])
    result = digest.plan(cfg)   # must not raise
    assert len(result["entries"]) == 1


def test_journal_tolerates_non_dict_marked_key(cfg):
    cfg.mb_dir.mkdir(parents=True)
    (cfg.mb_dir / "digest.json").write_text(json.dumps({"marked": "oops"}), encoding="utf-8")
    _commit(cfg, [(_chunk("learnings/active/T.md", "S1", "learning"), [1, 0])])
    result = digest.plan(cfg)
    assert len(result["entries"]) == 1


# --- stability ------------------------------------------------------------------

def test_repeat_plan_is_stable(cfg):
    pairs = [(_chunk("learnings/active/A.md", "A", "learning"), [1, 0]),
             (_chunk("learnings/active/B.md", "B", "learning"), [0.9, 0.1]),
             (_chunk("reference/R1.md", "R1", "reference"), [1, 0]),
             (_chunk("reference/R2.md", "R2", "reference"), [0.95, 0.05])]
    _commit(cfg, pairs)
    first = digest.plan(cfg)
    second = digest.plan(cfg)
    assert first == second


def test_never_mutates_notes(cfg, tmp_path):
    notes_dir = tmp_path / "learnings" / "active"
    notes_dir.mkdir(parents=True)
    note = notes_dir / "T.md"
    original = "---\ntype: learning\n---\n\n## S1\nbody\n"
    note.write_text(original, encoding="utf-8")
    _commit(cfg, [
        (_chunk("learnings/active/T.md", "S1", "learning"), [1, 0]),
        (_chunk("reference/R.md", "R1", "reference"), [1, 0]),
    ])
    digest.plan(cfg)
    digest.mark(cfg, [("learnings/active/T.md", "S1")])
    assert note.read_text(encoding="utf-8") == original
