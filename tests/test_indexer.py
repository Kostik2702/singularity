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
        "# Note A\n\n## Section one\n" + "useful content of the section " * 5,
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
        "# Note A\n\n## Section one\n" + "changed content of the section " * 5 +
        "\n\n## New section\n" + "a completely new section with text " * 5,
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


# --- C01: a Corpus can describe a NON-markdown body -------------------------

def _code_tree(tmp_path: Path) -> Path:
    """A miniature repositories root with one file per filter rule."""
    root = tmp_path / "repos"
    (root / "api" / "node_modules" / "pkg").mkdir(parents=True)
    (root / "api" / "db" / "migrations").mkdir(parents=True)
    (root / "api" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "api" / "widget.ts").write_text("export const a = 1\n", encoding="utf-8")
    (root / "api" / "widget.test.ts").write_text("it('x', () => {})\n", encoding="utf-8")
    (root / "api" / "bundle.min.js").write_text("var a=1\n", encoding="utf-8")
    (root / "api" / "types.d.ts").write_text("declare const a: number\n", encoding="utf-8")
    (root / "api" / "db" / "migrations" / "001.py").write_text("pass\n", encoding="utf-8")
    (root / "api" / "README.md").write_text("# doc\n", encoding="utf-8")
    (root / "api" / "node_modules" / "pkg" / "index.js").write_text("1\n", encoding="utf-8")
    return root


def _code_corpus(root: Path, index_dir: Path, **over) -> "indexer_mod.Corpus":
    kwargs = {
        "name": "code", "root": root, "index_dir": index_dir,
        "patterns": ("*.py", "*.ts", "*.js"),
        "prune_dirs": ("node_modules",),
        "skip_file_patterns": ("*.test.*", "*.min.js", "*.d.ts", "*/migrations/*"),
        "chunk_schema": 1,
    }
    kwargs.update(over)
    return indexer_mod.Corpus(**kwargs)


def test_scan_collects_only_the_declared_patterns(tmp_path):
    root = _code_tree(tmp_path)
    scanned = indexer_mod._scan_corpus(_code_corpus(root, tmp_path / "idx"))
    assert set(scanned) == {"api/app.py", "api/widget.ts"}
    assert not [k for k in scanned if k.endswith(".md")]


def test_scan_default_patterns_are_still_markdown_only(tmp_path):
    """The two live corpora must not notice C01 at all."""
    root = _code_tree(tmp_path)
    scanned = indexer_mod._scan_corpus(
        indexer_mod.Corpus(name="md", root=root, index_dir=tmp_path / "idx"))
    assert set(scanned) == {"api/README.md"}


def test_max_file_bytes_is_inclusive_at_the_boundary(tmp_path):
    root = tmp_path / "repos"
    root.mkdir()
    (root / "exact.py").write_text("a" * 100, encoding="utf-8")
    (root / "over.py").write_text("a" * 101, encoding="utf-8")
    corpus = indexer_mod.Corpus(name="code", root=root, index_dir=tmp_path / "idx",
                                patterns=("*.py",), max_file_bytes=100)
    assert set(indexer_mod._scan_corpus(corpus)) == {"exact.py"}


def test_skip_file_patterns_cut_only_what_they_name(tmp_path):
    root = _code_tree(tmp_path)
    corpus = _code_corpus(root, tmp_path / "idx", prune_dirs=())
    scanned = set(indexer_mod._scan_corpus(corpus))
    for cut in ("api/widget.test.ts", "api/bundle.min.js", "api/types.d.ts",
                "api/db/migrations/001.py"):
        assert cut not in scanned
    assert "api/widget.ts" in scanned and "api/app.py" in scanned


def test_prune_dirs_are_cut_at_any_depth(tmp_path):
    """`skip_dirs` only ever matched the FIRST path segment (chunking.should_skip),
    which cannot express "node_modules, wherever it is" on a root that holds
    many repositories."""
    root = _code_tree(tmp_path)
    scanned = set(indexer_mod._scan_corpus(_code_corpus(root, tmp_path / "idx")))
    assert not [k for k in scanned if "node_modules" in k]


def test_scan_never_stats_a_file_it_already_rejected(tmp_path, monkeypatch):
    """R4: the repositories root offers ~111 candidates per file actually wanted,
    so a `stat()` before the string filters is 111x the syscalls. The order is a
    contract, not a style choice — assert it directly."""
    root = _code_tree(tmp_path)
    statted: list[Path] = []
    real_stat = Path.stat

    def spy(self, *a, **kw):
        statted.append(Path(self))
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", spy)
    scanned = indexer_mod._scan_corpus(
        _code_corpus(root, tmp_path / "idx", max_file_bytes=10_000))
    monkeypatch.undo()

    interesting = {p.as_posix() for p in statted
                   if p.suffix in {".py", ".ts", ".js", ".md"}}
    survivors = {(root / rel).as_posix() for rel in scanned}
    assert interesting == survivors, "a rejected file was stat()ed anyway"


def test_scan_skips_a_file_that_vanishes_between_walk_and_stat(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    (root / "gone.py").write_text("x = 1\n", encoding="utf-8")
    real_stat = Path.stat

    def spy(self, *a, **kw):
        if Path(self).name == "gone.py":
            raise OSError("vanished")
        return real_stat(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", spy)
    corpus = indexer_mod.Corpus(name="code", root=root, index_dir=tmp_path / "idx",
                                patterns=("*.py",), max_file_bytes=10_000)
    assert indexer_mod._scan_corpus(corpus) == {}


# --- C01/D3: chunk_schema belongs to the corpus, not to markdown -------------

def test_bumping_the_markdown_schema_leaves_a_foreign_corpus_alone(tmp_path, monkeypatch):
    """The D3 defect in one assertion: `_commit_generation` stamped the global
    markdown constant onto EVERY corpus and `_update` compared against it, so a
    markdown-chunker bump forced a pointless full rebuild of an unrelated
    corpus (and a bump of THAT corpus's own chunker forced nothing)."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    root = tmp_path / "repos"
    root.mkdir()
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault", data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)

    def one_chunk(path, rel, personal_roots=None):
        return [{"file": rel, "text": f"code {rel}", "content": "x = 1"}]

    corpus = indexer_mod.Corpus(name="code", root=root,
                                index_dir=cfg.mb_dir / "code_index",
                                patterns=("*.py",), chunker=one_chunk,
                                chunk_schema=1)
    indexer_mod.rebuild(cfg, progress=False, corpus=corpus)
    current = (corpus.index_dir / "CURRENT").read_text(encoding="utf-8")

    from singularity.search import chunking
    monkeypatch.setattr(chunking, "CHUNK_SCHEMA_VERSION",
                        chunking.CHUNK_SCHEMA_VERSION + 1)
    stats = indexer_mod.update(cfg, corpus=corpus)

    assert stats == {"added": 0, "deleted": 0, "chunks": 1}     # no rebuild
    assert (corpus.index_dir / "CURRENT").read_text(encoding="utf-8") == current


def test_a_generation_stamped_by_the_old_global_schema_does_not_rebuild(tmp_path, monkeypatch):
    """Zero-downtime migration (D3): every generation already on disk carries the
    old GLOBAL number, and that is exactly what memory/sources now declare — so
    the first `mb index update` after the upgrade is an ordinary no-op diff, not
    a two-minute rebuild of the live vault."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    cfg = make_vault(tmp_path)
    indexer_mod.rebuild(cfg, progress=False)
    current = (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8")

    import json

    from singularity.search import chunking
    _, meta_path = indexer_mod.index_files(cfg.memory_index_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["chunk_schema"] == chunking.CHUNK_SCHEMA_VERSION
    assert meta["chunk_schema"] == indexer_mod.memory_corpus(cfg).chunk_schema

    stats = indexer_mod.update(cfg)
    assert stats == {"added": 0, "deleted": 0, "chunks": 2}
    assert (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8") == current


def test_only_files_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    cfg = make_vault(tmp_path)
    indexer_mod.rebuild(cfg, progress=False)

    import time
    time.sleep(0.01)
    for name in ("a.md", "b.md"):
        p = cfg.vault / "howto" / name
        p.write_text(p.read_text(encoding="utf-8") + " an addition at the very end of the text",
                     encoding="utf-8")
    stats = indexer_mod.update(cfg, only_files=["howto/a.md"])
    assert stats["added"] == 1  # b.md untouched because it is out of scope
