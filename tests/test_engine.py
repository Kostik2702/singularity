"""AUD-15: SearchEngine ranking/filtering/limits (no ML model — vectors are
hand-built and normalized, engine constructed without __init__/reload)."""
import numpy as np

from singularity.config import Config
from singularity.search.engine import SearchEngine


def _engine(tmp_path, chunks, embs):
    eng = SearchEngine.__new__(SearchEngine)      # skip __init__ (no model/reload)
    eng.cfg = Config(vault=tmp_path, data={"min_score": 0.30, "excerpt_chars": 150})
    eng.mem_embs, eng.mem_chunks = embs, chunks
    eng.code_embs, eng.code_chunks = None, []
    return eng


def test_match_filters_type_and_tags():
    m = {"type": "decision", "tags": "alpha,beta", "file": "decisions/x.md"}
    assert SearchEngine._match_filters(m, {"filter_type": "decision"})
    assert not SearchEngine._match_filters(m, {"filter_type": "howto"})
    assert SearchEngine._match_filters(m, {"filter_tags": ["beta"]})
    assert not SearchEngine._match_filters(m, {"filter_tags": ["gamma"]})


def test_match_filters_path_prefix_and_wikilinks():
    m = {"file": "learnings/active/T.md", "wikilinks": ["Other"], "title": "Self"}
    assert SearchEngine._match_filters(m, {"path_prefix": "learnings/"})
    assert not SearchEngine._match_filters(m, {"path_prefix": "decisions/"})
    assert SearchEngine._match_filters(m, {"filter_wikilinks": ["other"]})
    assert SearchEngine._match_filters(m, {"filter_wikilinks": ["self"]})  # self-link by title


def test_search_source_orders_by_score_and_applies_top_k(tmp_path):
    chunks = [{"file": "a.md", "type": "reference", "content": "one"},
              {"file": "b.md", "type": "reference", "content": "two"},
              {"file": "c.md", "type": "reference", "content": "three"}]
    embs = np.array([[1, 0, 0], [0, 1, 0], [0.6, 0.8, 0]], dtype=np.float32)
    eng = _engine(tmp_path, chunks, embs)
    q = np.array([1, 0, 0], dtype=np.float32)          # scores: 1.0, 0.0, 0.6
    res = eng._search_source(q, embs, chunks, "memory", {"top_k": 5})
    # 0.0 is below min_score 0.3 -> dropped; ordered desc
    assert [r["meta"]["file"] for r in res] == ["a.md", "c.md"]
    res1 = eng._search_source(q, embs, chunks, "memory", {"top_k": 1})
    assert [r["meta"]["file"] for r in res1] == ["a.md"]


def test_search_source_respects_min_score_cutoff(tmp_path):
    chunks = [{"file": "a.md"}, {"file": "b.md"}]
    embs = np.array([[1, 0], [0.2, 0.98]], dtype=np.float32)
    eng = _engine(tmp_path, chunks, embs)
    q = np.array([1, 0], dtype=np.float32)             # scores: 1.0, 0.2
    res = eng._search_source(q, embs, chunks, "memory", {"top_k": 5})
    assert [r["meta"]["file"] for r in res] == ["a.md"]   # 0.2 < 0.30 dropped


def test_search_source_filters_reduce_results(tmp_path):
    chunks = [{"file": "a.md", "type": "howto"},
              {"file": "b.md", "type": "decision"}]
    embs = np.array([[1, 0], [0.9, 0.44]], dtype=np.float32)
    eng = _engine(tmp_path, chunks, embs)
    q = np.array([1, 0], dtype=np.float32)
    res = eng._search_source(q, embs, chunks, "memory",
                             {"top_k": 5, "filter_type": "decision"})
    assert [r["meta"]["file"] for r in res] == ["b.md"]


def test_search_source_empty_index(tmp_path):
    eng = _engine(tmp_path, [], None)
    q = np.array([1, 0], dtype=np.float32)
    assert eng._search_source(q, None, [], "memory", {"top_k": 5}) == []
