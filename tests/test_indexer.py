"""Incremental-update contract tests with a mocked encoder (no torch needed)."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import singularity.search.indexer as indexer_mod
from singularity.config import Config


class FakeEncoder:
    """Deterministic 8-dim hash embedding."""

    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.random(8).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


def make_vault(tmp_path: Path) -> Config:
    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text(
        "# Заметка А\n\n## Секция один\n" + "полезное содержание секции " * 5,
        encoding="utf-8")
    (vault / "howto" / "b.md").write_text(
        "# Note B\n\n## Section two\n" + "meaningful english content here " * 5,
        encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def test_rebuild_and_incremental(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    cfg = make_vault(tmp_path)

    stats = indexer_mod.rebuild(cfg, progress=False)
    assert stats["files"] == 2 and stats["chunks"] == 2
    emb_path, _ = indexer_mod.index_files(cfg.memory_index_dir)
    embs = np.load(emb_path, allow_pickle=False)
    assert embs.shape == (2, 8)

    # no changes -> no-op
    stats = indexer_mod.update(cfg)
    assert stats["added"] == 0 and stats["deleted"] == 0

    # modify one file -> only it re-chunks
    import time
    time.sleep(0.01)
    (cfg.vault / "howto" / "a.md").write_text(
        "# Заметка А\n\n## Секция один\n" + "изменённое содержание секции " * 5 +
        "\n\n## Новая секция\n" + "совершенно новая секция с текстом " * 5,
        encoding="utf-8")
    stats = indexer_mod.update(cfg)
    assert stats["added"] == 1 and stats["deleted"] == 0 and stats["chunks"] == 3

    # delete a file -> its chunks removed
    (cfg.vault / "howto" / "b.md").unlink()
    stats = indexer_mod.update(cfg)
    assert stats["deleted"] == 1 and stats["chunks"] == 2

    emb_path, _ = indexer_mod.index_files(cfg.memory_index_dir)
    embs = np.load(emb_path, allow_pickle=False)
    assert embs.shape[0] == 2


# --- T04: project metadata + chunk-schema rebuild policy --------------------

def test_rebuild_stamps_project_from_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    (vault / "reference").mkdir(parents=True)
    (vault / "reference" / "tagged.md").write_text(
        "---\nproject: singularity\n---\n\n# T\n\n## S\n" + "content here " * 5,
        encoding="utf-8")
    (vault / "reference" / "untagged.md").write_text(
        "# U\n\n## S\n" + "other content here " * 5, encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False)

    import json
    _, meta_path = indexer_mod.index_files(cfg.memory_index_dir)
    chunks = json.loads(meta_path.read_text(encoding="utf-8"))["chunks"]
    by_file = {c["file"]: c for c in chunks}
    assert by_file["reference/tagged.md"]["project"] == "singularity"
    assert "project" not in by_file["reference/untagged.md"]


def test_update_falls_back_to_rebuild_on_chunk_schema_mismatch(tmp_path, monkeypatch):
    """A pre-T04 index (chunk_schema=2, no `project` key) must never be mixed
    incrementally with post-T04 chunks — update() must detect the version bump
    and do a full rebuild instead (indexer.py's existing model-mismatch path,
    now exercised for chunk_schema specifically)."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    cfg = make_vault(tmp_path)
    indexer_mod.rebuild(cfg, progress=False)

    import json

    from singularity.search import chunking
    _, meta_path = indexer_mod.index_files(cfg.memory_index_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["chunk_schema"] = chunking.CHUNK_SCHEMA_VERSION - 1     # simulate stale index
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    stats = indexer_mod.update(cfg)
    assert "files" in stats and "added" not in stats   # took _rebuild()'s shape, not _update()'s diff
    _, meta_path2 = indexer_mod.index_files(cfg.memory_index_dir)
    assert json.loads(meta_path2.read_text(encoding="utf-8"))["chunk_schema"] == \
        chunking.CHUNK_SCHEMA_VERSION


def test_only_files_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    cfg = make_vault(tmp_path)
    indexer_mod.rebuild(cfg, progress=False)

    import time
    time.sleep(0.01)
    for name in ("a.md", "b.md"):
        p = cfg.vault / "howto" / name
        p.write_text(p.read_text(encoding="utf-8") + " добавка в самый конец текста",
                     encoding="utf-8")
    stats = indexer_mod.update(cfg, only_files=["howto/a.md"])
    assert stats["added"] == 1  # b.md untouched because it is out of scope
