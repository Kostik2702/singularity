"""`code_chunk_schema` and the grammar pins (CODE-GRAPH D1 row "Chunker v2", P7).

The acceptance of P7, each clause one test here, each seen red on a plant:

* the schema is chosen by config, never by whether a parser is installed;
* the memory corpus's CURRENT is byte-identical across v1 -> v2 and v2 -> v1;
* a planted change of the PIN forces the code corpus -- and only it -- to
  rebuild;
* two interpreters with different grammars taking turns: 0 rebuilds, 1 refusal;
* v2 with a grammar that cannot load: refusal, CURRENT untouched, a WARN.

"Interpreter" is simulated the way it differs in life: by what installed
metadata says was loaded (`loaded_grammar_versions`), while the extractor
code itself is the real one.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import singularity.search.indexer as indexer_mod
from singularity.codegraph.versions import PARSER_PACKAGES
from singularity.config import DEFAULT_CONFIG, Config, validate_config
from singularity.search import code_chunking, daemon
from singularity.search import code_chunking_v2 as v2

REPO = Path(__file__).resolve().parents[1]


class DigestEncoder:
    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 16), dtype=np.float32)
        for i, text in enumerate(texts):
            digest = hashlib.sha256(str(text).encode("utf-8")).digest()
            vec = np.frombuffer(digest[:16], dtype=np.uint8).astype(np.float32) + 1.0
            out[i] = vec / np.linalg.norm(vec)
        return out


def _lock_text(pins: dict[str, str]) -> str:
    """A lock in the shape of requirements-graph.txt: a pin per package, the
    hash on a continuation line."""
    return "".join(f"{pkg}=={ver} \\\n    --hash=sha256:{'0' * 64}\n" for pkg, ver in pins.items())


def _tree(directory: Path) -> dict[str, bytes]:
    """Every file under an index directory, by relative path, with its bytes."""
    return {p.relative_to(directory).as_posix(): p.read_bytes()
            for p in sorted(directory.rglob("*")) if p.is_file()}


def _meta(index_dir: Path) -> dict:
    _, meta_path = indexer_mod.index_files(index_dir)
    return json.loads(meta_path.read_text(encoding="utf-8"))


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A memory corpus and a v1 code corpus, both built; the lock and the
    interpreter's loaded versions under the test's control."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: DigestEncoder())
    root = tmp_path / "Project"
    (root / "api").mkdir(parents=True)
    (root / "api" / "orders.py").write_text(
        "import json\n\n\nclass OrderBook:\n    \"\"\"Orders by id.\"\"\"\n\n"
        "    def add(self, order):\n        self.items[order.id] = order\n\n"
        "    def total(self):\n        return sum(o.amount for o in self.items.values())\n",
        encoding="utf-8")
    (root / "api" / "widget.ts").write_text(
        "export class WidgetView {\n  render(): string {\n    return this.props.label;\n  }\n}\n",
        encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "n.md").write_text(
        "# Note\n\n## Section\n" + "a note about the vault and nothing else " * 3, encoding="utf-8")

    pins = v2.pinned_grammar_versions()               # the real lock's pins
    lock = tmp_path / "requirements-graph.txt"
    lock.write_text(_lock_text(pins), encoding="utf-8")
    monkeypatch.setattr(v2, "PINS_FILE", lock)
    state = {"loaded": dict(pins)}
    monkeypatch.setattr(v2, "loaded_grammar_versions", lambda: dict(state["loaded"]))

    cfg = Config(vault=vault, data={"model": "fake", "code_repos_dir": str(root)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    return SimpleNamespace(cfg=cfg, root=root, lock=lock, pins=pins, state=state)


def _spy_rebuilds(monkeypatch) -> list[str]:
    calls: list[str] = []
    real = indexer_mod._rebuild

    def spy(cfg, progress=True, corpus=None):
        calls.append(indexer_mod.resolve_corpus(cfg, corpus).name)
        return real(cfg, progress, corpus)

    monkeypatch.setattr(indexer_mod, "_rebuild", spy)
    return calls


def _poison_grammar(monkeypatch) -> None:
    """A grammar that cannot load, for real: the extractor is forgotten and its
    grammar module cannot be imported, the way a missing wheel behaves."""
    monkeypatch.delitem(sys.modules, "singularity.codegraph.extract_py", raising=False)
    monkeypatch.setitem(sys.modules, "tree_sitter_python", None)


# --- the key -----------------------------------------------------------------

def test_the_default_is_v1_and_only_v1_or_v2_validate():
    assert DEFAULT_CONFIG["code_chunk_schema"] == "v1"
    assert validate_config({"code_chunk_schema": "v2"}) == {"errors": [], "warnings": []}
    for bad in ("v3", 2, None, "V2"):
        assert any("code_chunk_schema" in e
                   for e in validate_config({"code_chunk_schema": bad})["errors"]), bad


def test_the_code_corpus_takes_its_chunker_from_the_config(tmp_path):
    base = {"code_repos_dir": str(tmp_path)}
    default = indexer_mod.code_corpus(Config(vault=tmp_path, data=base))
    assert default.chunker is code_chunking.extract_code_chunks
    assert default.chunk_schema == code_chunking.CODE_CHUNK_SCHEMA_VERSION
    assert default.pin_guard is None
    two = indexer_mod.code_corpus(Config(vault=tmp_path, data={**base, "code_chunk_schema": "v2"}))
    assert two.chunker is v2.extract_code_chunks
    assert two.chunk_schema == v2.CODE_CHUNK_SCHEMA_VERSION
    assert two.pin_guard is v2.writer_guard
    with pytest.raises(ValueError, match="code_chunk_schema"):
        indexer_mod.code_corpus(Config(vault=tmp_path, data={**base, "code_chunk_schema": "v3"}))


def test_no_other_corpus_has_a_pin_guard(tmp_path):
    cfg = Config(vault=tmp_path, data={"code_chunk_schema": "v2"})
    assert indexer_mod.memory_corpus(cfg).pin_guard is None
    assert indexer_mod.sources_corpus(cfg).pin_guard is None


def test_the_schema_is_never_chosen_by_the_parser_being_there(world, monkeypatch):
    """UNMET (r). The parser is importable here, and the corpus is still v1
    because nobody configured v2; with v2 configured and the parser gone, the
    write refuses instead of quietly producing a v1 corpus."""
    meta = _meta(world.cfg.code_index_dir)
    assert meta["chunk_schema"] == code_chunking.CODE_CHUNK_SCHEMA_VERSION
    assert indexer_mod.GRAMMAR_PINS_KEY not in meta
    before = _tree(world.cfg.code_index_dir)
    world.cfg.data["code_chunk_schema"] = "v2"
    _poison_grammar(monkeypatch)
    with pytest.raises(v2.GrammarPinRefused):
        indexer_mod.update(world.cfg, corpus="code")
    assert _tree(world.cfg.code_index_dir) == before


# --- the pin source ----------------------------------------------------------

def _pyproject_graph_pins() -> dict[str, str]:
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"^graph\s*=\s*\[(.*?)\]", text, re.S | re.M)
    assert block, "pyproject has no `graph` extra"
    return dict(re.findall(r'"([A-Za-z0-9._-]+)==([^"]+)"', block.group(1)))


def test_the_lock_pins_every_parser_package_and_agrees_with_pyproject():
    """The lock is the one source v2 reads; the pyproject extra is a second
    copy of the same pins, and two copies that may differ are two sources."""
    pins = v2.pinned_grammar_versions(REPO / "requirements-graph.txt")
    assert list(pins) == list(PARSER_PACKAGES)
    extra = _pyproject_graph_pins()
    assert {p: extra.get(p) for p in PARSER_PACKAGES} == pins


def test_a_missing_or_incomplete_lock_refuses(tmp_path):
    with pytest.raises(v2.GrammarPinRefused, match="cannot be read"):
        v2.pinned_grammar_versions(tmp_path / "absent.txt")
    partial = tmp_path / "partial.txt"
    partial.write_text(_lock_text({"tree-sitter": "0.26.0", "tree_sitter_python": "0.25.0"}),
                       encoding="utf-8")
    with pytest.raises(v2.GrammarPinRefused, match="tree-sitter-typescript"):
        v2.pinned_grammar_versions(partial)


def test_the_loaded_versions_are_installed_metadata():
    loaded = v2.loaded_grammar_versions()
    assert list(loaded) == list(PARSER_PACKAGES)
    assert all(isinstance(v, str) and v for v in loaded.values())


# --- switching: only the code corpus moves -----------------------------------

def test_v2_stamps_the_pins_and_v1_stamps_nothing(world):
    world.cfg.data["code_chunk_schema"] = "v2"
    indexer_mod.update(world.cfg, corpus="code")
    meta = _meta(world.cfg.code_index_dir)
    assert meta["chunk_schema"] == v2.CODE_CHUNK_SCHEMA_VERSION
    assert meta[indexer_mod.GRAMMAR_PINS_KEY] == world.pins
    assert any(c.get("qname") == "api/orders.py::OrderBook.total" for c in meta["chunks"])
    world.cfg.data["code_chunk_schema"] = "v1"
    indexer_mod.update(world.cfg, corpus="code")
    meta = _meta(world.cfg.code_index_dir)
    assert meta["chunk_schema"] == code_chunking.CODE_CHUNK_SCHEMA_VERSION
    assert indexer_mod.GRAMMAR_PINS_KEY not in meta
    assert not any("qname" in c for c in meta["chunks"])


def test_switching_the_chunker_either_way_leaves_memory_byte_identical(world, monkeypatch):
    rebuilds = _spy_rebuilds(monkeypatch)
    memory_before = _tree(world.cfg.memory_index_dir)
    code_current = (world.cfg.code_index_dir / "CURRENT").read_bytes()
    for schema in ("v2", "v1"):
        world.cfg.data["code_chunk_schema"] = schema
        indexer_mod.update(world.cfg, corpus="code")
        indexer_mod.update(world.cfg)                    # memory: nothing to do
        assert _tree(world.cfg.memory_index_dir) == memory_before, schema
        now = (world.cfg.code_index_dir / "CURRENT").read_bytes()
        assert now != code_current, schema               # the code corpus did move
        code_current = now
    assert rebuilds == ["code", "code"]


def test_a_planted_pin_change_rebuilds_the_code_corpus_only(world, monkeypatch):
    world.cfg.data["code_chunk_schema"] = "v2"
    indexer_mod.update(world.cfg, corpus="code")
    rebuilds = _spy_rebuilds(monkeypatch)
    memory_before = _tree(world.cfg.memory_index_dir)

    assert indexer_mod.update(world.cfg, corpus="code")["added"] == 0
    assert rebuilds == []                                # unchanged pins: incremental

    moved = {**world.pins, "tree-sitter-python": world.pins["tree-sitter-python"] + ".post1"}
    world.lock.write_text(_lock_text(moved), encoding="utf-8")      # the lock moves...
    world.state["loaded"] = dict(moved)                            # ...and the wheels with it
    indexer_mod.update(world.cfg, corpus="code")
    assert rebuilds == ["code"]
    assert _meta(world.cfg.code_index_dir)[indexer_mod.GRAMMAR_PINS_KEY] == moved
    assert _tree(world.cfg.memory_index_dir) == memory_before

    indexer_mod.update(world.cfg, corpus="code")
    assert rebuilds == ["code"]                          # once per pin change, not per run


def test_two_interpreters_in_turn_mean_zero_rebuilds_and_one_refusal(world, monkeypatch):
    world.cfg.data["code_chunk_schema"] = "v2"
    indexer_mod.update(world.cfg, corpus="code")         # A builds v2
    rebuilds = _spy_rebuilds(monkeypatch)
    pinned = dict(world.pins)
    other = {**pinned, "tree-sitter-typescript": "0.21.0"}
    refusals = 0
    orders = world.root / "api" / "orders.py"
    for turn, loaded in enumerate((pinned, other, pinned)):
        world.state["loaded"] = loaded
        orders.write_text(orders.read_text(encoding="utf-8") + f"\n\nTURN_{turn} = {turn}\n",
                          encoding="utf-8")
        before = _tree(world.cfg.code_index_dir)
        try:
            stats = indexer_mod.update(world.cfg, corpus="code")
        except v2.GrammarPinRefused as exc:
            refusals += 1
            assert "tree-sitter-typescript 0.21.0 (pinned" in str(exc)
            assert _tree(world.cfg.code_index_dir) == before           # CURRENT untouched
        else:
            assert stats["added"] == 1                                 # incremental, not a rebuild
    assert (rebuilds, refusals) == ([], 1)
    assert _meta(world.cfg.code_index_dir)[indexer_mod.GRAMMAR_PINS_KEY] == pinned


def test_v2_with_an_unloadable_grammar_refuses_touches_nothing_and_warns(world, monkeypatch,
                                                                        caplog):
    world.cfg.data["code_chunk_schema"] = "v2"
    indexer_mod.update(world.cfg, corpus="code")
    code_before = _tree(world.cfg.code_index_dir)
    memory_before = _tree(world.cfg.memory_index_dir)
    _poison_grammar(monkeypatch)
    with caplog.at_level(logging.WARNING, logger="mb"):
        for write in (lambda: indexer_mod.update(world.cfg, corpus="code"),
                      lambda: indexer_mod.rebuild(world.cfg, progress=False, corpus="code")):
            with pytest.raises(v2.GrammarPinRefused, match="cannot load"):
                write()
    assert _tree(world.cfg.code_index_dir) == code_before
    assert _tree(world.cfg.memory_index_dir) == memory_before
    assert "WARNING" in caplog.text and "cannot load" in caplog.text


def test_a_refused_first_build_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(v2, "loaded_grammar_versions",
                        lambda: dict.fromkeys(PARSER_PACKAGES, "absent"))
    (tmp_path / "Project" / "api").mkdir(parents=True)
    (tmp_path / "Project" / "api" / "a.py").write_text("def a_long_enough_name():\n    return 1\n",
                                                       encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault", data={"model": "fake", "code_chunk_schema": "v2",
                                                 "code_repos_dir": str(tmp_path / "Project")})
    with pytest.raises(v2.GrammarPinRefused, match="absent"):
        indexer_mod.rebuild(cfg, progress=False, corpus="code")
    assert not cfg.code_index_dir.exists()


# --- the refusal reaches the user --------------------------------------------

def test_the_daemon_names_the_refusal(world):
    world.cfg.data["code_chunk_schema"] = "v2"
    world.state["loaded"] = {**world.pins, "tree-sitter": "0.20.0"}
    d = daemon.SearchDaemon(world.cfg)
    d.engine = type("E", (), {"reload": lambda self: {}})()
    out = d._handle({"cmd": "index", "action": "update", "corpus": "code"})
    assert out["error_type"] == "GrammarPinRefused"
    assert "tree-sitter 0.20.0" in out["error"]


def test_the_cli_exits_2_on_the_refusal(world, monkeypatch, capsys):
    from singularity import cli
    from singularity.search import client

    def refused(*_a, **_k):
        raise client.RemoteIndexError("parser differs from the pins",
                                      error_type="GrammarPinRefused")

    monkeypatch.setattr(client, "run_index", refused)
    args = cli._build_parser().parse_args(["index", "update", "--code"])
    assert cli.cmd_index(world.cfg, args) == 2
    assert "parser differs from the pins" in capsys.readouterr().err
