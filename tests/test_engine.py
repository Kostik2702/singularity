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


# --- T04: project filter ------------------------------------------------------

def test_match_filters_project_exact_and_general_default():
    tagged = {"file": "a.md", "project": "singularity"}
    untagged = {"file": "b.md"}
    assert SearchEngine._match_filters(tagged, {"filter_project": "singularity"})
    assert not SearchEngine._match_filters(tagged, {"filter_project": "other"})
    assert SearchEngine._match_filters(untagged, {"filter_project": "general"})
    assert not SearchEngine._match_filters(untagged, {"filter_project": "singularity"})


def test_match_filters_project_accepts_list_or_comma_string():
    m = {"file": "a.md", "project": "b"}
    assert SearchEngine._match_filters(m, {"filter_project": ["a", "b"]})
    assert SearchEngine._match_filters(m, {"filter_project": "a,b"})
    assert not SearchEngine._match_filters(m, {"filter_project": "a,c"})


def test_search_source_project_filter_widens_candidate_pool(tmp_path):
    # has_filters must include filter_project so a project-only filter doesn't
    # silently starve valid top-k results. "mine.md" deliberately ranks LAST by
    # raw score (29 "other" chunks score 1.0, "mine" scores ~0.31, just above
    # min_score) — reachable only through the wide (has_filters) pool of 100,
    # never the narrow unfiltered pool of top_k*3=15.
    chunks = [{"file": "mine.md", "project": "mine"}] + \
             [{"file": f"other{i}.md", "project": "other"} for i in range(29)]
    embs = np.vstack([[0.31, 0.95]] + [[1.0, 0.0]] * 29).astype(np.float32)
    eng = _engine(tmp_path, chunks, embs)
    q = np.array([1, 0], dtype=np.float32)
    res = eng._search_source(q, embs, chunks, "memory",
                             {"top_k": 5, "filter_project": "mine"})
    assert [r["meta"]["file"] for r in res] == ["mine.md"]


def test_compact_line_appends_project_when_present():
    line = SearchEngine._compact_line(
        {"meta": {"file": "a.md", "type": "howto", "project": "singularity",
                 "content": "c"}, "score": 0.9, "source": "memory"})
    assert "[howto@singularity]" in line


def test_compact_line_omits_project_when_absent():
    line = SearchEngine._compact_line(
        {"meta": {"file": "a.md", "type": "howto", "content": "c"},
         "score": 0.9, "source": "memory"})
    assert "[howto]" in line and "@" not in line


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


# --- AUD-17: diversity, strict top, explain ---------------------------------

def test_per_file_diversity_caps_chunks_per_file(tmp_path):
    # 3 chunks from the same file (AUD-03 windows) must not flood results
    chunks = [{"file": "big.md", "content": f"w{i}"} for i in range(3)] + \
             [{"file": "other.md", "content": "o"}]
    embs = np.array([[1, 0], [0.99, 0.14], [0.98, 0.2], [0.9, 0.44]], dtype=np.float32)
    eng = _engine(tmp_path, chunks, embs)
    eng.encoder = type("E", (), {"encode": staticmethod(
        lambda q: np.array([[1, 0]], dtype=np.float32))})()
    out = eng.search({"query": "x", "top_k": 5, "max_per_file": 2, "format": "json"})
    files = [r["meta"]["file"] for r in out["results"]]
    assert files.count("big.md") == 2          # capped at 2, not 3


def test_strict_global_top_k(tmp_path):
    chunks = [{"file": f"{i}.md", "content": "c"} for i in range(6)]
    embs = np.eye(6, dtype=np.float32)[:, :2]
    embs = np.array([[1, 0]] * 6, dtype=np.float32)
    eng = _engine(tmp_path, chunks, embs)
    eng.encoder = type("E", (), {"encode": staticmethod(
        lambda q: np.array([[1, 0]], dtype=np.float32))})()
    out = eng.search({"query": "x", "top_k": 3, "max_per_file": 0, "format": "json"})
    assert len(out["results"]) == 3            # exactly top_k, not top_k*sources


def test_explain_adds_component_scores(tmp_path):
    chunks = [{"file": "a.md", "content": "c"}]
    embs = np.array([[1, 0]], dtype=np.float32)
    eng = _engine(tmp_path, chunks, embs)
    eng.encoder = type("E", (), {"encode": staticmethod(
        lambda q: np.array([[1, 0]], dtype=np.float32))})()
    out = eng.search({"query": "x", "top_k": 5, "explain": True, "format": "json"})
    assert "explain" in out["results"][0]
    assert "semantic" in out["results"][0]["explain"]
