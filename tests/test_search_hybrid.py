"""BM25 half, RRF fusion, and the evaluation that says whether it helps.

AUD-17 asked for two things, and the second is the one that keeps the first
honest: a hybrid ranking is easy to add and hard to justify, so this file
carries a small evaluation set of queries whose right answer is known, and
asserts the property that actually matters — **fusion must not lose what the
vector half already found, and must find the exact-token cases it misses.**

The engine is exercised through a fake encoder: embeddings are deterministic
and semantic-ish (a bag-of-words projection), so "the vector half" behaves like
a real one for ranking purposes without loading a model.
"""
from __future__ import annotations

import numpy as np
import pytest

from singularity.config import Config
from singularity.search import lexical
from singularity.search.engine import SearchEngine

# (file, section, content) — a miniature vault with two deliberate traps:
# an identifier that appears in exactly one note, and two notes that are
# semantically close but only one of which mentions the flag.
CORPUS = [
    ("howto/daemon.md", "Reload",
     "The daemon rereads its configuration when asked to reload the settings."),
    ("howto/index.md", "Locking",
     "Only one writer may hold the index lock; a second one raises "
     "IndexLockedError and retries later."),
    ("reference/flags.md", "Gaps",
     "The per-signal ceiling limits how many items each signal contributes; "
     "raise it with --per-signal."),
    ("reference/gaps.md", "Signals",
     "Six signals feed the research agenda: unprocessed sources, open "
     "questions, missing pages, undigested learnings."),
    ("decisions/ports.md", "Port",
     "Port zero means the operating system picks a free port for the daemon."),
]

# query -> the file that MUST be found. Two of these are exact-token queries,
# which is precisely where a pure embedding search is weak.
EVAL = [
    ("IndexLockedError", "howto/index.md"),
    ("--per-signal", "reference/flags.md"),
    ("daemon rereads configuration", "howto/daemon.md"),
    ("which signals feed the agenda", "reference/gaps.md"),
]


class _BagOfWordsEncoder:
    """Deterministic stand-in for the real encoder.

    Projects tokens into a fixed random basis and normalises, so cosine
    similarity behaves like a semantic model for ranking purposes: related
    wording scores high, an unseen identifier scores near-nothing — which is
    the weakness the lexical half exists to cover.
    """

    def __init__(self, dim: int = 64):
        self.dim = dim
        self._rng = np.random.default_rng(7)
        self._basis: dict[str, np.ndarray] = {}

    def _vec(self, token: str) -> np.ndarray:
        if token not in self._basis:
            self._basis[token] = self._rng.normal(size=self.dim)
        return self._basis[token]

    def encode(self, texts, **_kw):
        out = []
        for text in texts:
            tokens = lexical.tokenize(text)
            v = np.sum([self._vec(t) for t in tokens], axis=0) if tokens else np.zeros(self.dim)
            norm = np.linalg.norm(v)
            out.append(v / norm if norm else v)
        return np.array(out, dtype=np.float32)


@pytest.fixture()
def engine(tmp_path, monkeypatch) -> SearchEngine:
    cfg = Config(vault=tmp_path, data={"model": "fake", "min_score": 0.30})
    monkeypatch.setattr("singularity.search.engine.shared_encoder",
                        lambda *a, **kw: _BagOfWordsEncoder())
    eng = SearchEngine(cfg)
    chunks = [{"file": f, "section": s, "content": c, "text": f"{f} - {s}: {c}",
               "type": "howto", "chunk_id": f"{f}#0.0"} for f, s, c in CORPUS]
    eng.mem_chunks = chunks
    eng.mem_embs = eng.encoder.encode([c["text"] for c in chunks])
    return eng


def _search(engine: SearchEngine, query: str, hybrid: bool) -> list[str]:
    """Files hit, best first. `format="json"` because the default compact form
    returns rendered lines, and a ranking assertion should read the data."""
    out = engine.search({"query": query, "top_k": 3, "hybrid": hybrid,
                         "memory_only": True, "format": "json"})
    return [r["meta"]["file"] for r in out.get("results", [])]


# --- BM25 itself ------------------------------------------------------------

def test_bm25_ranks_the_document_holding_a_rare_token_first():
    bm = lexical.BM25(c for _, _, c in CORPUS)
    top = lexical.rank_by_score(bm.scores("IndexLockedError"))[0]
    assert CORPUS[top][0] == "howto/index.md"


def test_bm25_tokenizer_keeps_identifiers_and_handles_cyrillic():
    assert lexical.tokenize("mb config set --per-signal") == [
        "mb", "config", "set", "per", "signal"]
    assert lexical.tokenize("Демон перечитывает конфиг") == [
        "демон", "перечитывает", "конфиг"]


def test_bm25_scores_are_zero_for_terms_the_corpus_never_saw():
    bm = lexical.BM25(c for _, _, c in CORPUS)
    assert bm.scores("квантовая телепортация") == [0.0] * len(CORPUS)


def test_rrf_rewards_agreement_between_rankings():
    """A document both halves like must beat one that only tops a single list."""
    fused = lexical.rrf([[5, 1, 2], [1, 9, 5]])
    assert max(fused, key=fused.get) == 1


# --- fusion inside the engine ----------------------------------------------

@pytest.mark.parametrize("query,expected", EVAL)
def test_hybrid_finds_every_evaluation_case(engine, query, expected):
    assert expected in _search(engine, query, hybrid=True), (
        f"hybrid search lost the known answer for {query!r}")


def test_hybrid_is_what_makes_the_exact_token_cases_work(engine):
    """The evaluation set earns its keep here: these two queries are the reason
    the lexical half exists, and the vector half alone misses at least one."""
    vector_only = [expected in _search(engine, q, hybrid=False)
                   for q, expected in EVAL if q in ("IndexLockedError", "--per-signal")]
    assert not all(vector_only), (
        "the fake encoder became good enough at identifiers that this test no "
        "longer measures anything — pick a harder token")


def test_hybrid_never_loses_a_semantic_answer_the_vector_half_had(engine):
    """The one regression that would make fusion a bad trade."""
    for query, expected in EVAL:
        if expected in _search(engine, query, hybrid=False):
            assert expected in _search(engine, query, hybrid=True), (
                f"fusion dropped {expected!r}, which plain vector search found "
                f"for {query!r}")


def test_hybrid_is_off_unless_asked(engine, monkeypatch):
    """Ranking order is a habit; changing it must be opt-in (config or flag)."""
    assert engine._hybrid_wanted({}) is False
    engine.cfg.data["hybrid_search"] = True
    assert engine._hybrid_wanted({}) is True
    assert engine._hybrid_wanted({"hybrid": False}) is False


def test_the_lexical_index_is_built_once_and_reused(engine):
    _search(engine, "IndexLockedError", hybrid=True)
    first = engine._bm25["memory"][1]
    _search(engine, "--per-signal", hybrid=True)
    assert engine._bm25["memory"][1] is first, "BM25 index rebuilt on every query"


def test_the_index_rebuilds_when_the_corpus_changes(engine):
    _search(engine, "IndexLockedError", hybrid=True)
    engine.mem_chunks = engine.mem_chunks[:2]
    engine.mem_embs = engine.mem_embs[:2]
    _search(engine, "IndexLockedError", hybrid=True)
    assert engine._bm25["memory"][1].n == 2, "stale BM25 index served after a reload"
