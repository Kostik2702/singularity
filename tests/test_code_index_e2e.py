"""C07: the whole code-index cycle on a throwaway tree, plus the isolation
regression that the epic treats as its main gate.

The encoder here is a deterministic bag-of-words over a tiny vocabulary rather
than the usual hash-noise fake: an end-to-end test whose "search" cannot tell
`retry` from `invoice` proves the pipes are connected and nothing else.
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import singularity.search.indexer as indexer_mod
from singularity.config import Config
from singularity.search.engine import SearchEngine

VOCAB = ["retry", "backoff", "invoice", "widget", "cancel", "note", "vault"]


class WordEncoder:
    """Unit-normalized presence vector over VOCAB — cosine actually means
    'shares vocabulary', so a query for `retry` really does rank the retry
    symbol first."""

    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), len(VOCAB)), dtype=np.float32)
        for i, text in enumerate(texts):
            low = str(text).lower()
            vec = np.array([1.0 if w in low else 0.0 for w in VOCAB],
                           dtype=np.float32)
            norm = np.linalg.norm(vec)
            out[i] = vec / norm if norm else vec
        return out


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: WordEncoder())
    repos = tmp_path / "Project"
    (repos / "api").mkdir(parents=True)
    (repos / "api" / "retry.py").write_text(
        "def retry_with_backoff(attempts):\n"
        '    """Retry the call with exponential backoff."""\n'
        "    return attempts\n", encoding="utf-8")
    (repos / "api" / "billing.py").write_text(
        "def build_invoice(order):\n"
        '    """Build an invoice for one order."""\n'
        "    return order\n", encoding="utf-8")

    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "n.md").write_text(
        "# Note\n\n## Section\n" + "a note about the vault and nothing else " * 3,
        encoding="utf-8")

    cfg = Config(vault=vault, data={"model": "fake", "min_score": 0.05,
                                    "excerpt_chars": 150,
                                    "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False)
    return cfg


def _engine(cfg):
    eng = SearchEngine.__new__(SearchEngine)
    eng.cfg = cfg
    eng.encoder = WordEncoder()
    eng.reload()
    return eng


def _search(cfg, query, **req):
    out = _engine(cfg).search({"query": query, "top_k": 5, "format": "json", **req})
    return [r["meta"]["file"] for r in out.get("results", [])], out


def test_full_cycle_build_find_change_find_delete_gone(world):
    cfg = world

    # 1. build
    stats = indexer_mod.rebuild(cfg, progress=False, corpus="code")
    assert stats["files"] == 2

    # 2. the symbol is findable — but only when asked for
    files, _ = _search(cfg, "retry backoff")
    assert "api/retry.py" not in files, "code must stay out of the default set"
    files, out = _search(cfg, "retry backoff", include_code=True)
    assert files[0] == "api/retry.py"
    assert out["results"][0]["meta"]["name"] == "retry_with_backoff"
    assert out["results"][0]["source"] == "code"

    # 3. change a file -> only it re-chunks, and the NEW text is findable
    time.sleep(0.01)
    (cfg.code_repos_dir / "api" / "billing.py").write_text(
        "def build_invoice(order):\n"
        '    """Build an invoice and cancel the previous widget draft."""\n'
        "    return order\n", encoding="utf-8")
    upd = indexer_mod.update(cfg, corpus="code")
    assert upd["added"] == 1 and upd["deleted"] == 0
    files, _ = _search(cfg, "cancel widget", include_code=True)
    assert files[0] == "api/billing.py"

    # 4. delete -> its chunks leave the index and the search
    (cfg.code_repos_dir / "api" / "retry.py").unlink()
    upd = indexer_mod.update(cfg, corpus="code")
    assert upd["deleted"] == 1
    files, _ = _search(cfg, "retry backoff", include_code=True)
    assert "api/retry.py" not in files


def test_repos_filter_end_to_end(world):
    cfg = world
    (cfg.code_repos_dir / "web").mkdir()
    (cfg.code_repos_dir / "web" / "retry.ts").write_text(
        "// Retry with backoff on the client too.\n"
        "export const retryFetch = async (url) => { return url; };\n",
        encoding="utf-8")
    indexer_mod.rebuild(cfg, progress=False, corpus="code")

    files, _ = _search(cfg, "retry backoff", include_code=True)
    assert {"api/retry.py", "web/retry.ts"} <= set(files)

    files, _ = _search(cfg, "retry backoff", include_code=True, repos="web")
    assert "api/retry.py" not in files and "web/retry.ts" in files


def test_code_only_never_returns_a_note(world):
    cfg = world
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    files, out = _search(cfg, "vault note", code_only=True)
    assert all(r["source"] == "code" for r in out["results"])
    assert "howto/n.md" not in files


# --- the isolation regression the epic calls its main gate -------------------

def _analytics(cfg):
    """curator + digest output, the two things that read the memory index."""
    from singularity import curator, digest
    return (json.dumps(curator.analyze(cfg), ensure_ascii=False, sort_keys=True),
            json.dumps(digest.plan(cfg), ensure_ascii=False, sort_keys=True))


def test_building_code_changes_neither_the_memory_pointer_nor_its_analytics(world):
    cfg = world
    before_current = (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8")
    before_curator, before_digest = _analytics(cfg)

    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    time.sleep(0.01)
    (cfg.code_repos_dir / "api" / "retry.py").write_text(
        "def retry_with_backoff(attempts, base):\n    return attempts\n",
        encoding="utf-8")
    indexer_mod.update(cfg, corpus="code")

    after_current = (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8")
    after_curator, after_digest = _analytics(cfg)

    assert after_current == before_current
    assert after_curator == before_curator
    assert after_digest == before_digest


def test_a_memory_update_leaves_the_code_generation_alone(world):
    cfg = world
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    code_current = (cfg.code_index_dir / "CURRENT").read_text(encoding="utf-8")

    time.sleep(0.01)
    (cfg.vault / "howto" / "n.md").write_text(
        "# Note\n\n## Section\n" + "an edited note about the vault entirely " * 3,
        encoding="utf-8")
    assert indexer_mod.update(cfg)["added"] == 1

    assert (cfg.code_index_dir / "CURRENT").read_text(encoding="utf-8") == code_current


def test_the_two_corpora_keep_independent_chunk_schemas(world):
    cfg = world
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    from singularity.search import chunking, code_chunking

    mem = indexer_mod.index_summary(cfg.memory_index_dir)
    code = indexer_mod.index_summary(cfg.code_index_dir)
    assert mem["chunk_schema"] == chunking.CHUNK_SCHEMA_VERSION
    assert code["chunk_schema"] == code_chunking.CODE_CHUNK_SCHEMA_VERSION


def test_line_numbers_survive_the_round_trip_to_the_index(world):
    """A chunk's line span must still address the real file after it has been
    through the writer — this is what makes a code hit clickable."""
    cfg = world
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    _, meta_path = indexer_mod.index_files(cfg.code_index_dir)
    for chunk in json.loads(meta_path.read_text(encoding="utf-8"))["chunks"]:
        src = (cfg.code_repos_dir / chunk["file"]).read_text(encoding="utf-8")
        sliced = "\n".join(src.splitlines()[chunk["line_start"] - 1:chunk["line_end"]])
        assert sliced.strip("\n") == chunk["content"]
