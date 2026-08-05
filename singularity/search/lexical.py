"""BM25 over the chunks already in memory — the half a vector cannot do.

An embedding search answers "what is this ABOUT", which is why `mb search`
finds a note about reload semantics when asked about "the daemon not picking up
config". It is also why it can miss an exact token: an identifier
(`IndexLockedError`), a flag (`--per-signal`), a filename or a version string
carries almost no semantic mass, so the nearest neighbours of a query built
around one are frequently the wrong notes. AUD-17 asked for the lexical half.

Nothing is precomputed at load: the index is built on first use and reused, so
a session that never runs a hybrid query pays nothing and the daemon's start
time is unchanged.

BM25 parameters are the standard ones (k1=1.5, b=0.75). They are not exposed as
config on purpose — tuning them without an evaluation set is guessing, and the
evaluation set (`tests/fixtures/search_eval.json`) is small enough that any
change it cannot justify should not be made.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any
from collections.abc import Iterable

# Unicode-aware: Cyrillic and Latin alike. `_` is kept because identifiers are
# exactly the case this module exists for (`chunk_id`, `min_score`).
_TOKEN_RE = re.compile(r"[^\W\d_]+[\w_]*|\d+[\w_]*", re.UNICODE)

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


class BM25:
    """Okapi BM25 over a fixed list of documents, in the order they were given.

    `scores()` returns one float per document, aligned with that order, so a
    caller can zip it against the chunk list it already holds.
    """

    def __init__(self, documents: Iterable[str]):
        self.docs: list[list[str]] = [tokenize(d) for d in documents]
        self.doc_len = [len(d) for d in self.docs]
        self.n = len(self.docs)
        self.avgdl = (sum(self.doc_len) / self.n) if self.n else 0.0
        self.freqs: list[Counter] = [Counter(d) for d in self.docs]
        df: Counter = Counter()
        for f in self.freqs:
            df.update(f.keys())
        # Robertson/Sparck-Jones idf with the +0.5 smoothing, floored at zero:
        # a term in nearly every document must not drag a score negative.
        self.idf = {term: max(0.0, math.log(1 + (self.n - c + 0.5) / (c + 0.5)))
                    for term, c in df.items()}

    def scores(self, query: str) -> list[float]:
        terms = [t for t in tokenize(query) if t in self.idf]
        if not terms or not self.n:
            return [0.0] * self.n
        out = [0.0] * self.n
        for i, freq in enumerate(self.freqs):
            if not freq:
                continue
            norm = K1 * (1 - B + B * (self.doc_len[i] / self.avgdl if self.avgdl else 0))
            total = 0.0
            for term in terms:
                tf = freq.get(term, 0)
                if tf:
                    total += self.idf[term] * (tf * (K1 + 1)) / (tf + norm)
            out[i] = total
        return out


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    """Reciprocal Rank Fusion of several rankings of the same document ids.

    Chosen over a weighted sum of the raw scores because cosine similarity and
    BM25 live on incomparable scales: a weighted sum needs a normalisation
    constant that has to be re-tuned whenever the corpus changes, while RRF only
    reads POSITIONS. `k` damps the head so one list cannot dominate on rank 1
    alone; 60 is the value from the original paper and is not worth tuning
    without an evaluation set larger than this project has.
    """
    fused: dict[int, float] = {}
    for ranking in rankings:
        for position, doc_id in enumerate(ranking):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + position + 1)
    return fused


def rank_by_score(scores: list[float], limit: int | None = None) -> list[int]:
    """Document ids ordered best-first, zeros dropped (they carry no signal)."""
    ranked = sorted((i for i, s in enumerate(scores) if s > 0),
                    key=lambda i: scores[i], reverse=True)
    return ranked[:limit] if limit else ranked


def document_text(chunk: dict[str, Any]) -> str:
    """What BM25 indexes for a chunk.

    `text` is what the embedder saw (title - section: content), so using it
    keeps the two halves looking at the same words — including the title and
    section, which is where identifiers usually appear.
    """
    return str(chunk.get("text") or chunk.get("content") or "")
