"""W03: the sources corpus (.mb/sources_index/) — a SECOND, physically
separate index.

The point of this file is the isolation guarantee: raw documents become
searchable on demand, and cannot reach the wiki analytics (`curator`,
`digest`) under any circumstance, because those read `memory_index` and the
vault scanner cannot even see `.mb/`.
"""
from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from singularity import curator, digest, sources
from singularity.config import Config
from singularity.search import client, indexer
from singularity.search.engine import SearchEngine


class FakeEncoder:
    """Deterministic 8-dim hash embedding (no torch)."""

    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.random(8).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    (vault / "reference").mkdir(parents=True)
    (vault / "learnings" / "active").mkdir(parents=True)
    (vault / "reference" / "note.md").write_text(
        "---\ntype: reference\ncreated: 2026-01-01\ntitle: Note\n---\n\n"
        "# Note\n\n## Section\n" + "vault knowledge about indexing pipelines " * 4,
        encoding="utf-8")
    (vault / "learnings" / "active" / "T.md").write_text(
        "---\ntype: learning\ncreated: 2026-01-01\ntitle: T\n---\n\n"
        "# T\n\n## Insight\n" + "a learning section long enough to chunk here " * 4,
        encoding="utf-8")
    c = Config(vault=vault, data={"model": "fake"})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


SECRET = ("Кодовое слово камнеломка встречается исключительно "
          "внутри проглоченного источника и нигде в vault.")


def _ingest(cfg, text=SECRET, **kw):
    kw.setdefault("origin_ref", "D:/docs/secret.md")
    return sources.add(cfg, "# Источник\n\n## Раздел\n" + text, **kw)


# --- corpus definition ------------------------------------------------------

def test_sources_corpus_paths(cfg):
    corp = indexer.sources_corpus(cfg)
    assert corp.name == "sources"
    assert corp.root == cfg.sources_dir
    assert corp.index_dir == cfg.sources_index_dir
    assert corp.rel_prefix == ".mb/sources/"


def test_resolve_corpus_accepts_name_instance_and_none(cfg):
    assert indexer.resolve_corpus(cfg, None).name == "memory"
    assert indexer.resolve_corpus(cfg, "sources").name == "sources"
    corp = indexer.sources_corpus(cfg)
    assert indexer.resolve_corpus(cfg, corp) is corp
    with pytest.raises(ValueError):
        indexer.resolve_corpus(cfg, "nope")


def test_scan_uses_vault_relative_paths_and_ignores_root_skip_rules(cfg):
    """`.mb` is in the vault's skip_dirs; applying those rules to the PREFIXED
    path would reject the whole sources corpus."""
    _ingest(cfg)
    scanned = indexer._scan_corpus(indexer.sources_corpus(cfg))
    assert len(scanned) == 1
    rel = next(iter(scanned))
    assert rel.startswith(".mb/sources/") and rel.endswith(".md")
    assert (cfg.vault / rel).is_file()


def test_scan_missing_root_is_empty_not_an_error(cfg):
    assert indexer._scan_corpus(indexer.sources_corpus(cfg)) == {}


def test_scan_vault_wrapper_still_returns_the_memory_corpus(cfg):
    _ingest(cfg)
    scanned = indexer._scan_vault(cfg)
    assert set(scanned) == {"reference/note.md", "learnings/active/T.md"}


# --- chunking ---------------------------------------------------------------

def test_source_chunks_are_typed_and_carry_source_id(cfg):
    result = _ingest(cfg)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    _, chunks, status = indexer.load_committed_index(cfg.sources_index_dir)
    assert status == "ok" and chunks
    assert {c["type"] for c in chunks} == {"source"}
    assert {c["source_id"] for c in chunks} == {result["source_id"]}
    assert all(c["file"].startswith(".mb/sources/") for c in chunks)


def test_raw_frontmatter_type_cannot_masquerade_as_a_note(cfg):
    """A raw document whose OWN frontmatter says type: reference must still
    index as a source."""
    sources.add(cfg, "---\ntype: reference\n---\n\n# Fake\n\n## S\n" + "x y " * 40,
                origin_ref="ref")
    indexer.rebuild(cfg, progress=False, corpus="sources")
    _, chunks, _ = indexer.load_committed_index(cfg.sources_index_dir)
    assert {c["type"] for c in chunks} == {"source"}


# --- isolation: the whole point ---------------------------------------------

def test_vault_corpus_never_indexes_sources(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False)
    _, chunks, _ = indexer.load_committed_index(cfg.memory_index_dir)
    assert chunks
    assert not any(c["file"].startswith(".mb") for c in chunks)
    assert not any(c.get("type") == "source" for c in chunks)


def test_memory_generation_is_untouched_by_ingest(cfg):
    indexer.rebuild(cfg, progress=False)
    current = (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8")

    _ingest(cfg)
    indexer.rebuild(cfg, progress=False, corpus="sources")

    assert (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8") == current


def test_curator_and_digest_output_is_byte_identical_before_and_after_sources(cfg):
    """The main W03 gate. Analytics read `memory_index`; a filled
    `.mb/sources/` must not move a single byte of their JSON."""
    indexer.rebuild(cfg, progress=False)
    before = (json.dumps(curator.validate(cfg), ensure_ascii=False, sort_keys=True),
              json.dumps(curator.analyze(cfg), ensure_ascii=False, sort_keys=True),
              json.dumps(curator.lint(cfg), ensure_ascii=False, sort_keys=True),
              json.dumps(digest.plan(cfg), ensure_ascii=False, sort_keys=True))

    for i in range(3):
        sources.add(cfg, f"# Doc {i}\n\n## S\n" + f"external material number {i} " * 8,
                    origin_ref=f"ref{i}")
    indexer.rebuild(cfg, progress=False, corpus="sources")

    after = (json.dumps(curator.validate(cfg), ensure_ascii=False, sort_keys=True),
             json.dumps(curator.analyze(cfg), ensure_ascii=False, sort_keys=True),
             json.dumps(curator.lint(cfg), ensure_ascii=False, sort_keys=True),
             json.dumps(digest.plan(cfg), ensure_ascii=False, sort_keys=True))
    assert after == before


def test_curator_scan_does_not_see_raw_files(cfg):
    _ingest(cfg)
    notes = curator._scan_notes(cfg)
    assert not any(rel.startswith(".mb") for rel in notes)


# --- search plumbing --------------------------------------------------------

def _engine(cfg):
    eng = SearchEngine.__new__(SearchEngine)
    eng.cfg = cfg
    eng.encoder = FakeEncoder()
    eng.reload()
    return eng


def test_source_only_phrase_needs_the_flag(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    eng = _engine(cfg)
    eng.cfg.data["min_score"] = -1.0        # FakeEncoder is a hash, not semantics

    plain = eng.search({"query": SECRET, "top_k": 20, "format": "json"})
    assert all(r["source"] != "sources" for r in plain["results"])

    with_sources = eng.search({"query": SECRET, "top_k": 20, "format": "json",
                               "include_sources": True})
    hits = [r for r in with_sources["results"] if r["source"] == "sources"]
    assert hits, "the sources corpus must be reachable with include_sources"
    assert hits[0]["meta"]["type"] == "source"
    assert hits[0]["meta"]["file"].startswith(".mb/sources/")


def test_compact_line_labels_the_corpus(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    eng = _engine(cfg)
    eng.cfg.data["min_score"] = -1.0
    out = eng.search({"query": SECRET, "top_k": 5, "include_sources": True})
    assert any("[source]" in line for line in out["lines"])


def test_reload_counts_include_sources(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    counts = _engine(cfg).reload()
    assert counts["sources"] > 0 and counts["memory"] > 0


def test_warning_when_nothing_is_ingested(cfg):
    indexer.rebuild(cfg, progress=False)
    out = _engine(cfg).search({"query": "anything", "include_sources": True})
    assert "no sources indexed" in out["sources_warning"]


def test_model_mismatch_disables_sources_with_a_warning(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")

    # rewrite the sources generation as if built by another model
    _, meta_path = indexer.index_files(cfg.sources_index_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["model"] = "some-other-model"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    eng = _engine(cfg)
    assert eng.src_embs is None and eng.src_chunks == []
    assert "some-other-model" in eng.sources_warning
    out = eng.search({"query": SECRET, "include_sources": True, "format": "json"})
    assert "some-other-model" in out["sources_warning"]
    assert all(r["source"] != "sources" for r in out["results"])


def test_kill_switch_keeps_the_corpus_out_of_the_engine(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    cfg.data["ingest_index_sources"] = False
    eng = _engine(cfg)
    assert eng.src_embs is None and eng.reload()["sources"] == 0


def test_client_run_index_forwards_the_corpus(tmp_path, monkeypatch):
    """Plumbing check: a dropped `corpus` here would silently index the vault
    instead of the sources."""
    seen = {}
    monkeypatch.setattr(client, "_read_port", lambda cfg: 7400)
    monkeypatch.setattr(client, "_request", lambda port, payload, **k:
                        seen.update(payload=payload) or
                        {"status": "indexed", "stats": {"added": 1}})
    client.run_index(Config(vault=tmp_path, data={}), "rebuild", corpus="sources")
    assert seen["payload"]["corpus"] == "sources"


def test_cold_fallback_preserves_include_sources(tmp_path, monkeypatch):
    """The cold path builds its own engine — the flag must survive it too."""
    seen = {}
    monkeypatch.setattr(client, "_read_port", lambda cfg: None)
    monkeypatch.setattr(client, "start_daemon", lambda cfg: False)

    class FakeEngine:
        def __init__(self, cfg):
            pass

        def search(self, payload):
            seen.update(payload)
            return {"lines": []}

    monkeypatch.setattr("singularity.search.engine.SearchEngine", FakeEngine)
    client.search(Config(vault=tmp_path, data={}),
                  {"cmd": "search", "query": "x", "include_sources": True})
    assert seen["include_sources"] is True


# --- locking ----------------------------------------------------------------

def test_per_corpus_locks_are_distinct(cfg):
    assert cfg.corpus_lock_dir("memory") == cfg.lock_dir
    assert cfg.corpus_lock_dir("sources") != cfg.lock_dir


def test_indexing_sources_is_not_blocked_by_a_held_vault_lock(cfg):
    _ingest(cfg)
    with indexer._writer_lock(cfg, "memory"):
        stats = indexer.rebuild(cfg, progress=False, corpus="sources")
    assert stats["files"] == 1

    with indexer._writer_lock(cfg, "sources"), pytest.raises(indexer.IndexLockedError):  # noqa: SIM117
        with indexer._writer_lock(cfg, "sources"):
            pass


def test_concurrent_corpus_writers_both_succeed(cfg):
    _ingest(cfg)
    results, errors = {}, []

    def run(corpus):
        try:
            results[corpus] = indexer.rebuild(cfg, progress=False, corpus=corpus)
        except BaseException as exc:  # noqa: BLE001
            errors.append((corpus, exc))

    threads = [threading.Thread(target=run, args=(c,)) for c in ("memory", "sources")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert set(results) == {"memory", "sources"}


# --- crash safety -----------------------------------------------------------

def test_torn_sources_generation_leaves_the_previous_one_serving(cfg):
    _ingest(cfg)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    good = (cfg.sources_index_dir / "CURRENT").read_text(encoding="utf-8")

    # simulate a crash mid-commit: a newer generation dir with no metadata.json
    torn = cfg.sources_index_dir / "gen_9999999999999_0_999999"
    torn.mkdir()
    np.save(torn / "embeddings.npy", np.zeros((1, 8), dtype=np.float32),
            allow_pickle=False)

    emb_path, _ = indexer.index_files(cfg.sources_index_dir)
    assert good in str(emb_path)
    _, chunks, status = indexer.load_committed_index(cfg.sources_index_dir)
    assert status == "ok" and chunks

    # and a torn CURRENT pointer falls back to the newest WHOLE generation
    (cfg.sources_index_dir / "CURRENT").write_text("gen_does_not_exist", encoding="utf-8")
    _, chunks, status = indexer.load_committed_index(cfg.sources_index_dir)
    assert status == "ok" and chunks


def test_sources_update_is_incremental(cfg):
    first = _ingest(cfg)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    stats = indexer.update(cfg, corpus="sources")
    assert stats["added"] == 0 and stats["deleted"] == 0

    time.sleep(0.01)
    second = sources.add(cfg, "# Second\n\n## S\n" + "another external document " * 6,
                         origin_ref="ref2")
    stats = indexer.update(cfg, only_files=[second["stored_path"]], corpus="sources")
    assert stats["added"] == 1
    _, chunks, _ = indexer.load_committed_index(cfg.sources_index_dir)
    assert {c["source_id"] for c in chunks} == {first["source_id"], second["source_id"]}
