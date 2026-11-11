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

Run it: `python tools/search_eval.py run --hybrid both`. Until 2026-08-05 this
docstring cited the fixture while no such file existed, so the sentence above
read as evidence and was none. `tests/test_search_eval.py` now fails if the
fixture goes missing or loses its class quota.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any
from collections.abc import Callable, Iterable

# Unicode-aware: Cyrillic and Latin alike. `_` is kept because identifiers are
# exactly the case this module exists for (`chunk_id`, `min_score`).
_TOKEN_RE = re.compile(r"[^\W\d_]+[\w_]*|\d+[\w_]*", re.UNICODE)

K1 = 1.5
B = 0.75


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


# --- prose tokens: inflection folded, identifiers untouched ----------------
#
# Russian inflects, so the exact-token tokenizer above treats the genitive,
# nominative and instrumental of one noun as three unrelated words, and a
# query in one case misses a note
# written in another. A suffix stripper is enough here: BM25 needs the forms
# of one word to COLLIDE, not a linguistically correct lemma, and it costs no
# dependency. Measured on the 66-pair set, 2026-11-11, same session, stemming
# plus the weighted head in `prose_document_text`: recall@1 0.515 -> 0.606,
# recall@5 0.758 -> 0.788, no class moving down.
#
# A token that carries `_` or a digit is an identifier or a version and is
# never stemmed -- those are the exact tokens this module exists for.
_STEM_MIN_TOKEN = 5
_STEM_MIN_STEM = 3
_RU_RE = re.compile("[\u0430-\u044f\u0451]")
_RU_SUFFIX_RE = re.compile(
    r"(иями|ями|ами|ией|иям|ием|иях|ого|его|ому|ему|ыми|ими|ается|уется|ться|тся|"
    r"ость|ости|ение|ения|ению|ений|ая|яя|ое|ее|ые|ие|ый|ий|ой|ых|их|ую|юю|ом|ем|"
    r"ам|ям|ах|ях|ов|ев|ей|ть|ет|ют|ит|ят|ал|ил|ла|ло|ли|а|я|о|е|ы|и|у|ю|ь)$")
_EN_SUFFIX_RE = re.compile(r"(ings|ing|edly|ed|ies|es|s)$")


def stem(token: str) -> str:
    """Fold a lowercase prose token to a crude stem; identifiers pass through."""
    if len(token) < _STEM_MIN_TOKEN or "_" in token or any(ch.isdigit() for ch in token):
        return token
    suffix = _RU_SUFFIX_RE if _RU_RE.search(token) else _EN_SUFFIX_RE
    stemmed = suffix.sub("", token)
    return stemmed if len(stemmed) >= _STEM_MIN_STEM else token


def tokenize_prose(text: str) -> list[str]:
    """`tokenize` with inflection folded -- for notes, never for code."""
    return [stem(t) for t in tokenize(text)]


class BM25:
    """Okapi BM25 over a fixed list of documents, in the order they were given.

    `scores()` returns one float per document, aligned with that order, so a
    caller can zip it against the chunk list it already holds.
    """

    def __init__(self, documents: Iterable[str],
                 tokenizer: Callable[[str], list[str]] = tokenize):
        # The same tokenizer must read the documents and the query: a stemmed
        # index asked with unstemmed terms silently matches nothing.
        self.tokenize = tokenizer
        self.docs: list[list[str]] = [tokenizer(d) for d in documents]
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
        terms = [t for t in self.tokenize(query) if t in self.idf]
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


def coverage(bm: BM25, query: str, doc_id: int) -> float:
    """The share of the query's idf mass (terms the index knows) that document
    `doc_id` contains: 1.0 holds every query term, 0.0 none or no known term."""
    terms = {t for t in bm.tokenize(query) if t in bm.idf}
    total = sum(bm.idf[t] for t in terms)
    if not total:
        return 0.0
    return sum(bm.idf[t] for t in terms if t in bm.freqs[doc_id]) / total


def covered(bm: BM25, query: str, doc_ids: list[int], share: float) -> list[int]:
    """`doc_ids` narrowed to those holding at least `share` of the query's idf
    mass -- the documents BM25 actually MATCHED, as opposed to those that
    happen to contain one of its words.

    Rank fusion gives the lexical list a vote equal to the vector's, and a
    BM25 ranking is never empty: a query in another language than the note, or
    one phrased in other words, still ranks whatever shares a single common
    term with it, and those chunks then outvote the semantic answer. Weighing
    by idf rather than counting terms keeps a lone rare identifier
    (`IndexLockedError` among stop-words) above the bar by itself.
    """
    if share <= 0:
        return doc_ids
    return [i for i in doc_ids if coverage(bm, query, i) >= share]


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


def prose_document_text(chunk: dict[str, Any]) -> str:
    """What BM25 indexes for a NOTE chunk: the head counted three times.

    A note's path, title and section are where a person's query words usually
    are (a rule note is NAMED after what it rules), yet in `text` they weigh the
    same as any word of the body, and a long body mentioning the term in
    passing outranks the note that is ABOUT it. Repeating the head is the
    plain form of field weighting; `text` already holds title and section
    once, hence two more here. The path is added once: folder and file name
    carry tokens (`incidents`, a ticket id) the title may not.
    """
    head = f"{chunk.get('title') or ''} {chunk.get('section') or ''}"
    return f"{chunk.get('file') or ''} {head} {head} {document_text(chunk)}"


# --- the excerpt a result is shown with (here, not in the engine: the CLI
# cuts excerpts too and must not import numpy to do it) ----------------------

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def focus_excerpt(content: str, query: str, size: int) -> str:
    """The `size`-char stretch of `content` that holds the most query words.

    The excerpt is how a reader decides whether a hit is worth opening. The
    head of the chunk answers that only when the match happens to be there; a
    window is ~1000 chars and the excerpt 150, so usually it is not, and the
    reader either opens the file to find out or trusts a line that says
    nothing. Words are compared by stem, as BM25 above compares them.
    No query word in the chunk (a cross-lingual or purely semantic hit) leaves
    the head, exactly as before.
    """
    if len(content) <= size:
        return content
    wanted = {stem(t) for t in tokenize(query)}
    wanted = {t for t in wanted if len(t) >= 3}
    hits = [(m.start(), stem(m.group().lower()))
            for m in _WORD_RE.finditer(content)] if wanted else []
    hits = [(pos, st) for pos, st in hits if st in wanted]
    if not hits:
        return content[:size] + "..."
    best_start, best_score = 0, -1
    for i, (pos, _) in enumerate(hits):
        seen = {st for p, st in hits[i:] if p < pos + size}
        if len(seen) > best_score:               # earliest window wins a tie
            best_start, best_score = pos, len(seen)
    # lead in with a little context, then snap to a word start
    start = max(0, min(best_start - size // 5, len(content) - size))
    if start:
        sp = content.find(" ", start, best_start)
        start = sp + 1 if sp != -1 else best_start
    piece = content[start:start + size]
    return ("..." if start else "") + piece + ("..." if start + size < len(content) else "")
