"""C03: the code corpus end to end through the shared writer.

The point of these tests is NOT that indexing works — C01 already proved the
writer — but that the third corpus is genuinely isolated from the two live
ones: its own lock, its own generation pointer, its own chunk schema, and an
off switch that refuses instead of producing an empty index.
"""
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import singularity.search.indexer as indexer_mod
from singularity.config import Config
from singularity.search import code_chunking, daemon


class FakeEncoder:
    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.random(8).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


def _repos(tmp_path: Path) -> Path:
    """Two real repositories plus everything the filters must throw away."""
    root = tmp_path / "Project"
    (root / "api" / "handlers").mkdir(parents=True)
    (root / "api" / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "web" / "src").mkdir(parents=True)
    (root / "test-project" / "src").mkdir(parents=True)      # SKIP_REPOS
    (root / "api" / "__MACOSX" / "handlers").mkdir(parents=True)   # zip debris

    (root / "api" / "handlers" / "orders.py").write_text(
        "def create_order(payload):\n"
        '    """Create an order and return its id."""\n'
        "    return payload\n", encoding="utf-8")
    (root / "api" / "handlers" / "billing.py").write_text(
        "class Invoice:\n"
        '    """One invoice, immutable once issued."""\n'
        "    TAX = 0.2\n", encoding="utf-8")
    (root / "api" / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports = function pad(s) { return ' ' + s; };\n", encoding="utf-8")
    (root / "web" / "src" / "widget.ts").write_text(
        "// Renders the widget shell.\n"
        "export class WidgetView {\n"
        "  render() { return 'a string long enough to survive'; }\n"
        "}\n", encoding="utf-8")
    (root / "web" / "src" / "widget.test.ts").write_text(
        "it('renders', () => { expect(true).toBe(true); });\n", encoding="utf-8")
    (root / "web" / "src" / "vendor.min.js").write_text(
        "var a=1;var b=2;var c=3;\n", encoding="utf-8")
    (root / "web" / "src" / "huge.js").write_text(
        "// " + "x" * (code_chunking.MAX_FILE_BYTES + 10), encoding="utf-8")
    (root / "test-project" / "src" / "fixture.py").write_text(
        "def fixture_helper():\n    return 'ignored repository entirely'\n",
        encoding="utf-8")
    (root / "api" / "README.md").write_text("# not code\n", encoding="utf-8")
    # The AppleDouble twin a macOS archiver writes next to every file: same
    # name, `._` prefix, resource-fork bytes. It matches `*.py`, so without the
    # prune it is chunked as code — 22 such chunks were live in the index on
    # 2026-09-26, all from one unzipped repository.
    (root / "api" / "__MACOSX" / "handlers" / "._orders.py").write_text(
        "def appledouble_debris():\n    return 'resource fork, not source'\n",
        encoding="utf-8")
    return root


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    root = _repos(tmp_path)
    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text(
        "# Note\n\n## Section\n" + "some real note content here " * 5, encoding="utf-8")
    c = Config(vault=vault, data={"model": "fake", "code_repos_dir": str(root)})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


# --- the off switch ----------------------------------------------------------

def test_unset_code_repos_dir_refuses_instead_of_building_an_empty_index(tmp_path):
    c = Config(vault=tmp_path / "vault", data={"model": "fake"})
    assert c.code_repos_dir is None
    with pytest.raises(indexer_mod.CodeCorpusNotConfigured) as exc:
        indexer_mod.code_corpus(c)
    assert "code_repos_dir" in str(exc.value)
    assert not c.code_index_dir.exists()


def test_blank_code_repos_dir_is_treated_as_unset(tmp_path):
    c = Config(vault=tmp_path / "vault", data={"code_repos_dir": "   "})
    assert c.code_repos_dir is None


def test_code_repos_dir_expands_a_home_relative_value(tmp_path):
    c = Config(vault=tmp_path / "vault", data={"code_repos_dir": "~/code"})
    assert c.code_repos_dir == Path.home() / "code"


def test_cli_index_code_without_config_exits_2_and_creates_nothing(tmp_path, capsys):
    from singularity import cli
    c = Config(vault=tmp_path / "vault", data={})
    args = cli._build_parser().parse_args(["index", "rebuild", "--code"])
    assert cli.cmd_index(c, args) == 2
    assert "code_repos_dir" in capsys.readouterr().err
    assert not c.code_index_dir.exists()


def test_daemon_refuses_the_code_corpus_when_unconfigured(tmp_path):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = type("E", (), {"reload": lambda self: {}})()
    out = d._handle({"cmd": "index", "action": "rebuild", "corpus": "code"})
    assert out["error_type"] == "CodeCorpusNotConfigured"


def test_code_is_a_registered_corpus():
    assert indexer_mod.CORPUS_BUILDERS["code"] is indexer_mod.code_corpus


# --- what actually gets indexed ---------------------------------------------

def _chunks(cfg):
    _, meta_path = indexer_mod.index_files(cfg.code_index_dir)
    return json.loads(meta_path.read_text(encoding="utf-8"))["chunks"]


def test_rebuild_code_collects_exactly_the_wanted_files(cfg):
    stats = indexer_mod.rebuild(cfg, progress=False, corpus="code")
    files = {c["file"] for c in _chunks(cfg)}
    assert files == {"api/handlers/orders.py", "api/handlers/billing.py",
                     "web/src/widget.ts"}
    assert stats["files"] == 3 and stats["chunks"] >= 3


@pytest.mark.parametrize("excluded,why", [
    ("api/node_modules/left-pad/index.js", "pruned directory"),
    ("web/src/widget.test.ts", "skip_file_patterns"),
    ("web/src/vendor.min.js", "skip_file_patterns"),
    ("web/src/huge.js", "max_file_bytes"),
    ("test-project/src/fixture.py", "SKIP_REPOS (D1)"),
    ("api/__MACOSX/handlers/._orders.py", "macOS zip debris"),
    ("api/README.md", "not a code pattern"),
])
def test_rebuild_code_excludes(cfg, excluded, why):
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    assert excluded not in {c["file"] for c in _chunks(cfg)}, why


def test_repo_is_the_first_path_segment(cfg):
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    by_repo = {c["repo"] for c in _chunks(cfg)}
    assert by_repo == {"api", "web"}


def test_code_generation_records_the_code_chunk_schema(cfg):
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    summary = indexer_mod.index_summary(cfg.code_index_dir)
    assert summary["chunk_schema"] == code_chunking.CODE_CHUNK_SCHEMA_VERSION


# --- incremental ------------------------------------------------------------

def test_update_code_reindexes_only_the_changed_file(cfg):
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    time.sleep(0.01)
    (cfg.code_repos_dir / "api" / "handlers" / "orders.py").write_text(
        "def create_order(payload):\n"
        '    """Create an order, charge it, and return its id."""\n'
        "    return payload\n", encoding="utf-8")
    stats = indexer_mod.update(cfg, corpus="code")
    assert stats["added"] == 1 and stats["deleted"] == 0


def test_deleting_a_source_file_drops_its_chunks(cfg):
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    (cfg.code_repos_dir / "web" / "src" / "widget.ts").unlink()
    stats = indexer_mod.update(cfg, corpus="code")
    assert stats["deleted"] == 1
    assert "web/src/widget.ts" not in {c["file"] for c in _chunks(cfg)}


# --- isolation from the vault corpus ----------------------------------------

def test_rebuilding_the_vault_never_touches_the_code_generation(cfg):
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    code_current = (cfg.code_index_dir / "CURRENT").read_text(encoding="utf-8")

    indexer_mod.rebuild(cfg, progress=False)                 # memory corpus

    assert (cfg.code_index_dir / "CURRENT").read_text(encoding="utf-8") == code_current


def test_indexing_code_never_touches_the_memory_generation(cfg):
    indexer_mod.rebuild(cfg, progress=False)
    mem_current = (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8")

    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    time.sleep(0.01)
    (cfg.code_repos_dir / "api" / "handlers" / "orders.py").write_text(
        "def create_order(payload):\n    return None\n", encoding="utf-8")
    indexer_mod.update(cfg, corpus="code")

    assert (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8") == mem_current


def test_the_two_corpora_do_not_block_each_other(cfg):
    """Per-corpus writer lock (W03): a multi-minute code rebuild must not stall
    the watcher's on-save update of a note."""
    started = threading.Event()
    release = threading.Event()

    def hold_code_lock():
        with indexer_mod._writer_lock(cfg, "code"):
            started.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold_code_lock)
    t.start()
    try:
        assert started.wait(timeout=5)
        indexer_mod.rebuild(cfg, progress=False)     # memory: must NOT raise
        with pytest.raises(indexer_mod.IndexLockedError):
            indexer_mod.rebuild(cfg, progress=False, corpus="code")
    finally:
        release.set()
        t.join(timeout=5)


def test_code_lock_is_a_separate_path_from_the_memory_lock(cfg):
    assert cfg.corpus_lock_dir("code") != cfg.corpus_lock_dir("memory")
