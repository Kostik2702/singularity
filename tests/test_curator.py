"""T13: curator semantic lint (chunk-level near-duplicates, supersession
candidates, context.json budget/reference issues) + the T13 real-machine
finding that _note_embeddings() read a stale legacy flat index path instead
of the CURRENT committed generation -- see
[[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] T13.

No dedicated curator test file existed before this task (confirmed absent).
"""
from __future__ import annotations

import datetime as _dt
import json
import time

import numpy as np
import pytest

from singularity import curator
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
    chunks = [c for c, _ in pairs]
    embs = np.array([v for _, v in pairs], dtype=np.float32)
    indexer._commit_generation(
        cfg.memory_index_dir, embs,
        {"model": "m", "file_meta": {}, "chunks": chunks},
        chunk_schema=indexer.chunking.CHUNK_SCHEMA_VERSION)


def _note(vault, rel, body="# T\ncontent\n", created=None, mtime=None):
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = "---\ntype: reference\n"
    if created:
        fm += f"created: {created}\n"
    fm += "---\n\n"
    path.write_text(fm + body, encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


# --- T13 real-machine finding: stale flat index bypassed CURRENT generation --

def test_note_embeddings_uses_current_generation_not_stale_flat_file(cfg):
    d = cfg.memory_index_dir
    d.mkdir(parents=True)
    # a stale flat-layout leftover with DIFFERENT content than the current
    # committed generation -- the exact live-vault scenario found in T13
    stale_embs = np.array([[1, 0]], dtype=np.float32)
    np.save(d / "embeddings.npy", stale_embs, allow_pickle=False)
    (d / "metadata.json").write_text(json.dumps(
        {"chunks": [{"file": "reference/Stale.md", "section": "S"}]}), encoding="utf-8")
    # the REAL current generation, committed after the flat leftover
    _note(cfg.vault, "reference/Current.md")
    _commit(cfg, [(_chunk("reference/Current.md", "S1"), [1, 0])])
    files, vecs = curator._note_embeddings(cfg)
    assert files == ["reference/Current.md"]
    assert "reference/Stale.md" not in files


def test_note_embeddings_empty_when_index_missing(cfg):
    files, vecs = curator._note_embeddings(cfg)
    assert files == []
    assert vecs.shape[0] == 0


# --- blockwise duplicate detection --------------------------------------------

def test_duplicate_pairs_same_file_detected(cfg):
    _note(cfg.vault, "reference/A.md")
    _commit(cfg, [
        (_chunk("reference/A.md", "S1"), [1, 0]),
        (_chunk("reference/A.md", "S2"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
    ])
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert status == "ok"
    assert len(same_file) == 1
    assert same_file[0]["a"]["file"] == "reference/A.md"
    assert cross_file == []


def test_duplicate_pairs_cross_file_detected(cfg):
    _note(cfg.vault, "reference/A.md")
    _note(cfg.vault, "reference/B.md")
    _commit(cfg, [
        (_chunk("reference/A.md", "S1"), [1, 0]),
        (_chunk("reference/B.md", "S1"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
    ])
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert same_file == []
    assert len(cross_file) == 1
    assert {cross_file[0]["a"]["file"], cross_file[0]["b"]["file"]} == \
        {"reference/A.md", "reference/B.md"}


def test_duplicate_pairs_excludes_sequential_chunks_of_same_section(cfg):
    # a long section split into multiple chunk_index parts (same file, same
    # section, same line range) is a chunking artifact, not a real duplicate
    # -- confirmed live against the production vault (a 58-line section split
    # into 6 near-identical sub-chunks was the top "duplicate" hit).
    _commit(cfg, [
        (_chunk("reference/Long.md", "One Section"), [1, 0]),
        (_chunk("reference/Long.md", "One Section"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
    ])
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert same_file == []
    assert cross_file == []


def test_duplicate_pairs_below_threshold_not_reported(cfg):
    _commit(cfg, [
        (_chunk("reference/A.md", "S1"), [1, 0]),
        (_chunk("reference/B.md", "S1"), [0, 1]),   # orthogonal, score 0
    ])
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert same_file == [] and cross_file == []


def test_duplicate_pairs_excludes_personal_project(cfg):
    _commit(cfg, [
        (_chunk("reference/A.md", "S1", project="personal"), [1, 0]),
        (_chunk("reference/B.md", "S1", project="personal"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
    ])
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert cross_file == []


def test_duplicate_pairs_excludes_housekeeping_prefix(cfg):
    _commit(cfg, [
        (_chunk("changelog/2026-07.md", "S1"), [1, 0]),
        (_chunk("reference/B.md", "S1"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
    ])
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert cross_file == []


def test_duplicate_pairs_missing_index_reports_status(cfg):
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert status == "missing"
    assert same_file == [] and cross_file == []


def test_duplicate_pairs_torn_index_degrades_safely(cfg):
    d = cfg.memory_index_dir
    d.mkdir(parents=True)
    gen = d / "gen_torn"
    gen.mkdir()
    np.save(gen / "embeddings.npy", np.zeros((3, 2), dtype=np.float32), allow_pickle=False)
    (gen / "metadata.json").write_text(json.dumps({"chunks": [{"file": "a"}]}), encoding="utf-8")
    (d / "CURRENT").write_text("gen_torn", encoding="utf-8")
    same_file, cross_file, status = curator._duplicate_pairs(cfg)
    assert status == "torn"
    assert same_file == [] and cross_file == []


def test_duplicate_pairs_deterministic_repeat(cfg):
    _commit(cfg, [
        (_chunk("reference/A.md", "S1"), [1, 0]),
        (_chunk("reference/B.md", "S1"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
        (_chunk("reference/C.md", "S1"), [0.995, np.sqrt(1 - 0.995 ** 2)]),
    ])
    first = curator._duplicate_pairs(cfg)
    second = curator._duplicate_pairs(cfg)
    assert first == second


def test_blockwise_pairs_matches_brute_force_across_block_boundary():
    rng = np.random.default_rng(42)
    n = 20
    embs = rng.normal(size=(n, 8)).astype(np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    threshold = -1.0   # every pair qualifies -- exercises full coverage
    blockwise = set((i, j) for i, j, _ in curator._blockwise_pairs(embs, threshold, block_size=3))
    brute = set()
    for i in range(n):
        for j in range(i + 1, n):
            brute.add((i, j))
    assert blockwise == brute


def test_blockwise_pairs_bounded_memory_large_synthetic(cfg):
    # large-n smoke test -- must complete without materializing an n x n matrix
    rng = np.random.default_rng(1)
    n = 3000
    embs = rng.normal(size=(n, 16)).astype(np.float32)
    embs /= np.linalg.norm(embs, axis=1, keepdims=True)
    pairs = curator._blockwise_pairs(embs, threshold=0.999, block_size=256)
    assert isinstance(pairs, list)   # near-orthogonal random vectors -> few/no matches, no crash


# --- supersession candidates --------------------------------------------------

def test_supersession_flags_gap_over_60_days(cfg):
    now = time.time()
    older = _note(cfg.vault, "reference/Old.md", created="2026-01-01")
    newer = _note(cfg.vault, "reference/New.md", created="2026-07-01")
    notes = curator._scan_notes(cfg)
    pairs = [{"a": {"file": "reference/Old.md", "section": "S"},
             "b": {"file": "reference/New.md", "section": "S"}, "score": 0.95}]
    candidates = curator._supersession_candidates(notes, pairs)
    assert len(candidates) == 1
    assert candidates[0]["older"] == "reference/Old.md"
    assert candidates[0]["newer"] == "reference/New.md"
    assert candidates[0]["gap_days"] >= 60


def test_supersession_excludes_gap_under_60_days(cfg):
    _note(cfg.vault, "reference/A.md", created="2026-07-01")
    _note(cfg.vault, "reference/B.md", created="2026-07-10")
    notes = curator._scan_notes(cfg)
    pairs = [{"a": {"file": "reference/A.md", "section": "S"},
             "b": {"file": "reference/B.md", "section": "S"}, "score": 0.95}]
    assert curator._supersession_candidates(notes, pairs) == []


def test_supersession_falls_back_to_mtime_without_frontmatter_date(cfg):
    now = time.time()
    _note(cfg.vault, "reference/Old.md", mtime=now - 100 * 86400)
    _note(cfg.vault, "reference/New.md", mtime=now)
    notes = curator._scan_notes(cfg)
    pairs = [{"a": {"file": "reference/Old.md", "section": "S"},
             "b": {"file": "reference/New.md", "section": "S"}, "score": 0.95}]
    candidates = curator._supersession_candidates(notes, pairs)
    assert len(candidates) == 1
    assert candidates[0]["older"] == "reference/Old.md"


def test_supersession_one_candidate_per_file_pair(cfg):
    _note(cfg.vault, "reference/Old.md", created="2026-01-01")
    _note(cfg.vault, "reference/New.md", created="2026-07-01")
    notes = curator._scan_notes(cfg)
    pairs = [
        {"a": {"file": "reference/Old.md", "section": "S1"},
         "b": {"file": "reference/New.md", "section": "S1"}, "score": 0.98},
        {"a": {"file": "reference/Old.md", "section": "S2"},
         "b": {"file": "reference/New.md", "section": "S2"}, "score": 0.91},
    ]
    candidates = curator._supersession_candidates(notes, pairs)
    assert len(candidates) == 1
    assert candidates[0]["score"] == 0.98   # highest-scoring pair for that file pair, sort-first wins


def test_supersession_skips_pair_missing_from_notes(cfg):
    _note(cfg.vault, "reference/A.md", created="2026-01-01")
    notes = curator._scan_notes(cfg)
    pairs = [{"a": {"file": "reference/A.md", "section": "S"},
             "b": {"file": "reference/DoesNotExist.md", "section": "S"}, "score": 0.95}]
    assert curator._supersession_candidates(notes, pairs) == []


def test_frontmatter_date_handles_date_object_string_and_garbage():
    d = _dt.date(2026, 7, 1)
    assert curator._frontmatter_date(d) == _dt.datetime.combine(d, _dt.time.min).timestamp()
    assert curator._frontmatter_date("2026-07-01") is not None
    assert curator._frontmatter_date("not a date") is None
    assert curator._frontmatter_date(12345) is None
    assert curator._frontmatter_date(None) is None


# --- context.json budget / missing-note issues --------------------------------

def _write_context_json(vault, data):
    mb = vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    (mb / "context.json").write_text(json.dumps(data), encoding="utf-8")


def test_context_issues_empty_when_no_context_json(cfg):
    assert curator._context_issues(cfg) == []


def test_context_issues_missing_brain_note(cfg):
    _write_context_json(cfg.vault, {"proj": {"brain_note": "reference/DoesNotExist.md"}})
    issues = curator._context_issues(cfg)
    assert len(issues) == 1
    assert issues[0]["kind"] == "missing_note"
    assert issues[0]["project"] == "proj"


def test_context_issues_over_budget_brain_note(cfg):
    body = "# T\n" + "\n".join(f"line {i}" for i in range(100))
    _note(cfg.vault, "reference/Big.md", body=body)
    _write_context_json(cfg.vault, {"proj": {"brain_note": "reference/Big.md"}})
    cfg.data["brain_max_lines"] = 10
    issues = curator._context_issues(cfg)
    assert len(issues) == 1
    assert issues[0]["kind"] == "over_budget"
    assert issues[0]["lines"] > 10
    assert issues[0]["budget"] == 10


def test_context_issues_clean_when_within_budget(cfg):
    _note(cfg.vault, "reference/Small.md", body="# T\none line\n")
    _write_context_json(cfg.vault, {"proj": {"brain_note": "reference/Small.md"}})
    assert curator._context_issues(cfg) == []


def test_context_issues_module_note_missing(cfg):
    _write_context_json(cfg.vault, {
        "proj": {"context_map": {"mod": {"globs": ["src/"], "note": "reference/Gone.md"}}}})
    issues = curator._context_issues(cfg)
    assert len(issues) == 1
    assert issues[0]["kind"] == "missing_note"
    assert issues[0]["module"] == "mod"


def test_context_issues_malformed_json_degrades_safely(cfg):
    mb = cfg.vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    (mb / "context.json").write_text("{not json", encoding="utf-8")
    assert curator._context_issues(cfg) == []   # must not raise


def test_context_issues_path_traversal_rejected(cfg, tmp_path):
    outside = tmp_path.parent / "secret.md"
    outside.write_text("SECRET", encoding="utf-8")
    _write_context_json(cfg.vault, {"proj": {"brain_note": "../../secret.md"}})
    issues = curator._context_issues(cfg)
    assert len(issues) == 1
    assert issues[0]["kind"] == "missing_note"   # traversal resolves to None -> treated as missing


# --- lint() top-level + run() wiring -------------------------------------------

def test_lint_never_writes(cfg):
    _note(cfg.vault, "reference/A.md")
    _note(cfg.vault, "reference/B.md")
    _commit(cfg, [
        (_chunk("reference/A.md", "S1"), [1, 0]),
        (_chunk("reference/B.md", "S1"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
    ])
    before = {p: p.read_bytes() for p in cfg.vault.rglob("*.md")}
    curator.lint(cfg)
    after = {p: p.read_bytes() for p in cfg.vault.rglob("*.md")}
    assert before == after


def test_lint_shape(cfg):
    result = curator.lint(cfg)
    assert set(result) == {"index_status", "duplicates", "supersession", "context_issues"}
    assert set(result["duplicates"]) == {"same_file", "cross_file"}


def test_lint_caps_are_applied(cfg, monkeypatch):
    monkeypatch.setattr(curator, "DUPLICATE_LIMIT", 1)
    pairs = [
        (_chunk("reference/A.md", "S1"), [1, 0]),
        (_chunk("reference/B.md", "S1"), [0.999, np.sqrt(1 - 0.999 ** 2)]),
        (_chunk("reference/C.md", "S1"), [0.995, np.sqrt(1 - 0.995 ** 2)]),
    ]
    _commit(cfg, pairs)
    result = curator.lint(cfg)
    assert len(result["duplicates"]["cross_file"]) <= 1


def test_run_json_includes_lint(cfg, capsys):
    curator.run(cfg, as_json=True)
    payload = json.loads(capsys.readouterr().out.strip())
    assert "lint" in payload
    assert "duplicates" in payload["lint"]


def test_run_human_output_includes_lint_sections(cfg, capsys):
    curator.run(cfg, as_json=False)
    out = capsys.readouterr().out
    assert "Duplicate sections (same file" in out
    assert "Supersession candidates" in out
    assert "Context budget/reference issues" in out
