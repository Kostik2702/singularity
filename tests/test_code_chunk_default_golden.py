"""The default code corpus is the P0 code corpus, byte for byte (CODE-GRAPH P7).

`code_chunk_schema` defaults to "v1", and the acceptance of P7 is that nobody
who has not switched it sees any difference: the same chunks, the same
metadata keys, the same search answers. "Nothing changed" is only evidence if
it is compared with something recorded BEFORE the change, so the golden file
was written by this very function from the tree at dec1d60 (P4, the last
commit before the chunker v2 existed) and is never regenerated to make a red
test green -- a difference here is a behaviour change of the default path.

The encoder is a digest of the text, not the usual `hash()` noise: `hash()`
of a str is salted per process, so two runs of the same build would embed
differently and no search answer could be recorded at all.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import numpy as np

import singularity.search.engine as engine_mod
import singularity.search.indexer as indexer_mod
from singularity.config import Config
from singularity.search.engine import SearchEngine

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "code"
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "code_corpus_v1_golden.json"
QUERIES = ("retry with exponential backoff", "SessionStore", "render one widget",
           "evict expired sessions")


class DigestEncoder:
    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, text in enumerate(texts):
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            vec = np.frombuffer(digest[:16], dtype=np.uint8).astype(np.float32) + 1.0
            out[i] = vec / np.linalg.norm(vec)
        return out


def default_corpus_snapshot(tmp_path: Path, monkeypatch, data: dict | None = None) -> dict:
    """Build the code corpus over the fixture files with the config a machine
    has when it never mentioned `code_chunk_schema`, and record what a reader
    of that corpus gets."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: DigestEncoder())
    monkeypatch.setattr(engine_mod, "shared_encoder", lambda *_a, **_k: DigestEncoder())
    repo = tmp_path / "Project" / "demo"
    repo.mkdir(parents=True)
    for src in sorted(FIXTURES.iterdir()):
        shutil.copyfile(src, repo / src.name)
    cfg = Config(vault=tmp_path / "vault",
                 data={"model": "fake", "code_repos_dir": str(tmp_path / "Project"),
                       **(data or {})})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    _, meta_path = indexer_mod.index_files(cfg.code_index_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    engine = SearchEngine(cfg)
    search = {q: engine.search({"query": q, "top_k": 5, "code_only": True,
                                "include_code": True, "format": "json"})
              for q in QUERIES}
    return {"meta_keys": sorted(meta), "chunk_schema": meta["chunk_schema"],
            "chunks": meta["chunks"], "search": search}


def _ranked(answer: dict) -> list[tuple[str, str]]:
    return [(r["meta"]["file"], r["meta"]["chunk_id"]) for r in answer["results"]]


def test_the_default_code_corpus_is_the_p0_corpus(tmp_path, monkeypatch):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    now = default_corpus_snapshot(tmp_path, monkeypatch)
    assert now["chunk_schema"] == golden["chunk_schema"] == 1
    # no v2 key (grammar pins, qnames) leaks into a corpus that did not ask for v2
    assert now["meta_keys"] == golden["meta_keys"]
    assert now["chunks"] == golden["chunks"]


def test_the_default_search_answers_are_the_p0_answers(tmp_path, monkeypatch):
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    now = default_corpus_snapshot(tmp_path, monkeypatch)
    for query in QUERIES:
        assert _ranked(now["search"][query]) == _ranked(golden["search"][query]), query
        # the whole trimmed hit, not only its identity: an extra metadata key
        # would reach the user's screen through `_trim_meta`
        assert [r["meta"] for r in now["search"][query]["results"]] == \
               [r["meta"] for r in golden["search"][query]["results"]], query
        for a, b in zip(now["search"][query]["results"], golden["search"][query]["results"],
                        strict=True):
            assert abs(a["score"] - b["score"]) < 1e-4


def test_an_explicit_v1_is_the_default(tmp_path, monkeypatch):
    """Writing the default out (`Config.save` materialises defaults) must not
    be a different corpus from leaving it unset."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    now = default_corpus_snapshot(tmp_path, monkeypatch, {"code_chunk_schema": "v1"})
    assert now["chunks"] == golden["chunks"]
    assert now["meta_keys"] == golden["meta_keys"]
