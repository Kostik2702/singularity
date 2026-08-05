"""The declared shapes must match what the code actually produces.

A `TypedDict` that nobody checks is a comment with syntax highlighting: it goes
stale the first time someone adds a key, and then it misleads instead of
documenting. These tests build each structure through the real code path and
compare the keys, in both directions —

* a key the code emits but `contracts.py` does not declare means the contract
  is incomplete;
* a REQUIRED key the contract declares but the code never emits means the
  contract lies about what a consumer can rely on.

Optional keys (`wikilinks`, `source_id`, the code-corpus fields) are exempt
from the second check by construction: `total=False` is the statement that they
may be absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from singularity import contracts
from singularity.config import Config
from singularity.knowledge import contradictions, sources
from singularity.search import chunking

OPTIONAL = {
    "Chunk": {"wikilinks", "aliases", "repo", "symbol", "kind"},
    "SourceEntry": {"project", "supersedes", "superseded_by"},
    "ContradictionCandidate": set(),
    "SourceSide": {"source_id"},
    "VerdictRecord": {"note", "at"},
    "VerdictResult": set(),
}


def _declared(name: str) -> set[str]:
    return set(getattr(contracts, name).__annotations__)


def _check(name: str, produced: set[str]):
    declared = _declared(name)
    undeclared = sorted(produced - declared)
    assert not undeclared, (
        f"{name}: the code emits keys the contract does not declare: {undeclared}")
    missing = sorted(declared - produced - OPTIONAL[name])
    assert not missing, (
        f"{name}: the contract declares required keys the code never emits: "
        f"{missing} — either they are optional (move them to OPTIONAL) or the "
        "contract is describing a shape that no longer exists")


@pytest.fixture()
def cfg(tmp_path) -> Config:
    (tmp_path / ".mb").mkdir(parents=True)
    return Config(vault=tmp_path, data={"model": "fake"})


def test_chunk_contract_matches_the_indexer(tmp_path):
    note = tmp_path / "t.md"
    note.write_text("---\ntype: howto\ntitle: T\ntags: [a, b]\n---\n\n# T\n\n"
                    "## S\n\nSee [[Other]].\n" + "word " * 80, encoding="utf-8")
    chunks = chunking.extract_chunks(note, "howto/t.md")
    assert chunks, "fixture produced no chunks — the test would assert nothing"
    _check("Chunk", set().union(*(set(c) for c in chunks)))


def test_source_entry_contract_matches_the_registry(cfg):
    sources.add(cfg, "# Doc\n\n## S\n\n" + "text " * 40, origin_ref="ref")
    registry = json.loads(cfg.sources_registry_path.read_text(encoding="utf-8"))
    rows = registry.get("sources") or {}
    assert rows, "no source was registered — the test would assert nothing"
    _check("SourceEntry", set().union(*(set(r) for r in rows.values())))


def test_contradiction_and_side_contracts_match_the_scanner():
    a = {"file": "a.md", "section": "S", "source_id": "abc"}
    b = {"file": "b.md", "section": "S"}
    entry = contradictions._entry(a, b, score=0.8, cosine=0.81,
                                  evidence={"boolean_flip": ["on/off"]})
    _check("ContradictionCandidate", set(entry))
    _check("SourceSide", set(entry["a"]) | set(entry["b"]))


def test_verdict_contracts_match_the_answer_and_the_journal(cfg):
    """Two shapes, on purpose: the command answers with a count the stored row
    has no business carrying. Declaring one `Verdict` for both was wrong, and
    this test is what said so."""
    result = contradictions.record_verdict(cfg, "k", "false_positive", note="why")
    _check("VerdictResult", set(result))
    journal = json.loads(contradictions.journal_path(cfg).read_text(encoding="utf-8"))
    stored = journal["verdicts"]
    row = stored[next(iter(stored))] if isinstance(stored, dict) else stored[0]
    _check("VerdictRecord", set(row))


def test_contracts_are_typed_dicts_not_objects():
    """Runtime shape must stay `dict`: the index, `.mb/sources.json` and every
    `--json` payload are serialised straight from these values."""
    for name in OPTIONAL:
        cls = getattr(contracts, name)
        assert issubclass(cls, dict), f"{name} stopped being a dict at runtime"
        assert Path(contracts.__file__).name == "contracts.py"
