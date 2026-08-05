"""The shapes that cross module boundaries, written down.

Four dicts carry almost everything in this engine — a chunk, a registered
source, a contradiction candidate, a recorded verdict — and their fields were
documented in prose, asserted in tests, and reconstructed by eye everywhere
else. A missing key surfaced as a `KeyError` three layers away from whoever
wrote the dict.

These are `TypedDict`s ON PURPOSE, not dataclasses: at runtime they ARE plain
dicts, so nothing about serialisation, the index format, or the JSON that
skills and hooks already parse changes by a byte. What changes is that the
shape now lives in one place a type checker and a reader can both use.

`total=False` marks the genuinely optional keys — `wikilinks` and `aliases`
appear only when a note has them, and pretending otherwise would make the
contract lie in the other direction.
"""
from __future__ import annotations

from typing import Any, TypedDict


class Chunk(TypedDict, total=False):
    """One indexed window of a note or a code file (`search.chunking`).

    `file` is always VAULT-relative, including for the sources corpus
    (`.mb/sources/2026/x.md`) — the indexer prepends the corpus prefix so a
    chunk can be resolved with `Config.resolve` no matter which corpus built it.
    `chunk_id` is `<file>#<section-ordinal>.<window-ordinal>` and is what
    dedup, digest and the ingest fan-out join on.
    """
    file: str
    title: str
    section: str
    content: str
    text: str                 # what actually gets embedded: title - section: content
    type: str
    tags: str                 # comma-joined, not a list: it is stored as one cell
    chunk_index: int
    line_start: int
    line_end: int
    chunk_id: str
    wikilinks: list[str]
    aliases: list[str]
    # code corpus only
    repo: str
    symbol: str
    kind: str


class SourceSide(TypedDict, total=False):
    """One half of a contradiction pair — where the statement lives."""
    file: str
    section: str
    source_id: str            # present only when the chunk came from a source


class SourceEntry(TypedDict, total=False):
    """A row of `.mb/sources.json` — the one thing under `.mb/` a rebuild
    cannot recreate (WIKI-LOOP D1), which is why it is write-once and backed up
    with checksums. Keyed by `source_id` in the registry, so the id is not a
    field of the row itself."""
    title: str
    sha256: str
    bytes: int
    chars: int
    origin: dict[str, str]    # {kind, ref}
    stored_path: str          # vault-relative, resolve with Config.resolve
    stored: bool
    content_type: str
    project: str
    tags: list[str]
    ingested_at: str
    status: str               # new | processed
    derived_notes: list[str]
    marks: dict[str, Any]
    supersedes: str | None
    superseded_by: str | None


class ContradictionCandidate(TypedDict, total=False):
    """A PAIR, never a verdict (WIKI-LOOP W07): precision without an LLM is low
    by construction, so `evidence` — the tokens that triggered it — travels with
    the pair and the reader adjudicates."""
    key: str
    a: SourceSide
    b: SourceSide
    score: float
    cosine: float
    evidence: dict[str, list[str]]


class VerdictRecord(TypedDict, total=False):
    """A row stored under `verdicts` in `.mb/lint.json`, keyed by the pair key
    (so the key itself is not a field). Silences a pair for good — without it
    the same false positive resurfaces on every run and the report stops being
    read at all."""
    verdict: str              # false_positive | resolved | accepted
    note: str
    at: str


class VerdictResult(TypedDict):
    """What `mb curator verdict` ANSWERS — deliberately not the stored row: the
    caller wants confirmation plus how many verdicts now exist, the journal
    wants neither."""
    key: str
    verdict: str
    total: int
