"""C04: `--with-code` and `--repos` across every link of the chain.

A flag that is parsed but not forwarded is worse than a missing flag: it is
silently ignored, and the user reads the empty result as "no such code". So
each link gets its own test — CLI payload, client transport, cold fallback,
daemon dispatch, engine behavior — rather than one end-to-end test that could
pass while a middle link drops the key.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity import cli
from singularity.config import Config
from singularity.search import client, daemon
from singularity.search.engine import SearchEngine

CODE_CHUNKS = [
    {"file": "api/retry.py", "repo": "api", "kind": "function",
     "name": "retry_with_backoff", "content": "def retry_with_backoff(): ..."},
    {"file": "web/src/retry.ts", "repo": "web", "kind": "const",
     "name": "retryFetch", "content": "export const retryFetch = () => {}"},
]
MEM_CHUNKS = [{"file": "howto/retry.md", "type": "howto", "title": "Retry",
               "content": "how we retry"}]


def _engine(tmp_path, *, with_code=True):
    eng = SearchEngine.__new__(SearchEngine)
    eng.cfg = Config(vault=tmp_path, data={"min_score": 0.30, "excerpt_chars": 150})
    eng.mem_embs = np.array([[1.0, 0.0]], dtype=np.float32)
    eng.mem_chunks = MEM_CHUNKS
    if with_code:
        eng.code_embs = np.array([[0.99, 0.14], [0.98, 0.2]], dtype=np.float32)
        eng.code_chunks = CODE_CHUNKS
    else:
        eng.code_embs, eng.code_chunks = None, []
    eng.src_embs, eng.src_chunks = None, []
    eng.sources_warning = eng.code_warning = None
    eng.encoder = type("E", (), {"encode": staticmethod(
        lambda q, progress=False: np.array([[1.0, 0.0]], dtype=np.float32))})()
    return eng


def _files(out):
    return [r["meta"]["file"] for r in out["results"]]


# --- link 1: the CLI builds the payload --------------------------------------

def _payload_for(argv, cfg, monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "search",
                        lambda cfg_, payload: seen.update(payload) or {"lines": []})
    args = cli._build_parser().parse_args(argv)
    assert cli.cmd_search(cfg, args) == 0
    return seen


@pytest.fixture
def cfg_with_code(tmp_path):
    return Config(vault=tmp_path, data={"code_repos_dir": str(tmp_path / "repos")})


def test_cli_forwards_with_code_and_repos(cfg_with_code, monkeypatch):
    payload = _payload_for(["search", "retry", "--with-code", "--repos", "api,web"],
                           cfg_with_code, monkeypatch)
    assert payload["include_code"] is True
    assert payload["repos"] == "api,web"


def test_cli_defaults_leave_code_out(cfg_with_code, monkeypatch):
    payload = _payload_for(["search", "retry"], cfg_with_code, monkeypatch)
    assert payload["include_code"] is False
    assert payload["repos"] is None


def test_cli_refuses_with_code_when_the_corpus_is_off(tmp_path, capsys):
    args = cli._build_parser().parse_args(["search", "retry", "--with-code"])
    assert cli.cmd_search(Config(vault=tmp_path, data={}), args) == 2
    assert "code_repos_dir" in capsys.readouterr().err


def test_cli_refuses_code_only_when_the_corpus_is_off(tmp_path, capsys):
    args = cli._build_parser().parse_args(["search", "retry", "--code-only"])
    assert cli.cmd_search(Config(vault=tmp_path, data={}), args) == 2
    assert "code_repos_dir" in capsys.readouterr().err


def test_cli_prints_the_code_warning_to_stderr(cfg_with_code, monkeypatch, capsys):
    monkeypatch.setattr(client, "search", lambda cfg_, payload: {
        "lines": [], "code_warning": "no code index exists — build it"})
    args = cli._build_parser().parse_args(["search", "retry", "--with-code"])
    assert cli.cmd_search(cfg_with_code, args) == 0
    assert "no code index exists" in capsys.readouterr().err


# --- link 2: the client transports it verbatim -------------------------------

def test_client_sends_the_payload_unchanged(tmp_path, monkeypatch):
    sent = {}
    monkeypatch.setattr(client, "_read_port", lambda cfg: 4242)
    monkeypatch.setattr(client, "_request",
                        lambda port, payload, **kw: sent.update(payload) or {"lines": []})
    client.search(Config(vault=tmp_path, data={}),
                  {"cmd": "search", "include_code": True, "repos": "api"})
    assert sent["include_code"] is True and sent["repos"] == "api"


# --- link 3: the cold fallback keeps the flags -------------------------------

def test_cold_fallback_still_carries_the_flags(tmp_path, monkeypatch):
    """The fallback path builds its own engine; a payload key dropped here would
    only misbehave on machines where the daemon cannot start."""
    seen = {}
    monkeypatch.setattr(client, "_read_port", lambda cfg: None)
    monkeypatch.setattr(client, "start_daemon", lambda cfg: False)

    class FakeEngine:
        def __init__(self, cfg):
            pass

        def search(self, payload):
            seen.update(payload)
            return {"lines": []}

    monkeypatch.setattr("singularity.search.engine.SearchEngine", FakeEngine)
    out = client.search(Config(vault=tmp_path, data={}),
                        {"cmd": "search", "include_code": True, "repos": "api"})
    assert out["cold"] is True
    assert seen["include_code"] is True and seen["repos"] == "api"


# --- link 4: the daemon hands the request to the engine ----------------------

def test_daemon_passes_the_search_request_through(tmp_path):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    seen = {}
    d.engine = type("E", (), {"search": lambda self, req: seen.update(req) or {}})()
    d._handle({"cmd": "search", "query": "retry", "include_code": True,
               "repos": "api"})
    assert seen["include_code"] is True and seen["repos"] == "api"


# --- link 5: the engine actually behaves -------------------------------------

def test_code_is_absent_from_the_default_result_set(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5, "format": "json"})
    assert _files(out) == ["howto/retry.md"]


def test_with_code_brings_code_in(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5,
                                    "include_code": True, "format": "json"})
    assert set(_files(out)) == {"howto/retry.md", "api/retry.py", "web/src/retry.ts"}


def test_code_only_drops_memory(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5,
                                    "code_only": True, "format": "json"})
    assert set(_files(out)) == {"api/retry.py", "web/src/retry.ts"}


def test_memory_only_wins_over_with_code(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5, "include_code": True,
                                    "memory_only": True, "format": "json"})
    assert _files(out) == ["howto/retry.md"]


def test_repos_filter_narrows_to_one_repository(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5, "include_code": True,
                                    "repos": "api", "format": "json"})
    assert _files(out) == ["howto/retry.md", "api/retry.py"]


def test_repos_filter_tolerates_spaces_after_commas(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5, "include_code": True,
                                    "repos": "api, web", "format": "json"})
    assert set(_files(out)) == {"howto/retry.md", "api/retry.py", "web/src/retry.ts"}


def test_repos_does_not_silently_wipe_notes(tmp_path):
    """A note has no repository, so `--repos api` must not exclude it. The
    alternative — every note filtered out because it lacks a code-only field —
    looks identical to "the vault knows nothing about this"."""
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5,
                                    "repos": "api", "format": "json"})
    assert _files(out) == ["howto/retry.md"]


def test_repos_naming_no_real_repository_returns_no_code(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5, "code_only": True,
                                    "repos": "nope", "format": "json"})
    assert _files(out) == []


def test_compact_line_shows_kind_name_and_path_not_a_bare_path(tmp_path):
    out = _engine(tmp_path).search({"query": "retry", "top_k": 5,
                                    "code_only": True})
    assert any(ln.startswith("api/retry.py |") and "[function]" in ln
               and "retry_with_backoff" in ln for ln in out["lines"])


# --- model consistency between corpora ---------------------------------------

def test_a_code_index_built_by_another_model_is_refused(tmp_path, monkeypatch):
    """Cosine across two embedding spaces is nonsense; serving it quietly is
    worse than refusing. Same rule the sources corpus got in W05."""
    cfg = Config(vault=tmp_path, data={})
    eng = SearchEngine.__new__(SearchEngine)
    eng.cfg = cfg

    def fake_load(index_dir):
        if index_dir == cfg.code_index_dir:
            return np.ones((1, 2), dtype=np.float32), CODE_CHUNKS[:1], "other-model"
        if index_dir == cfg.memory_index_dir:
            return np.ones((1, 2), dtype=np.float32), MEM_CHUNKS, "vault-model"
        return None, [], None

    monkeypatch.setattr(SearchEngine, "_load_one", staticmethod(fake_load))
    eng.reload()
    assert eng.code_chunks == [] and "other-model" in eng.code_warning

    eng.encoder = type("E", (), {"encode": staticmethod(
        lambda q, progress=False: np.array([[1.0, 0.0]], dtype=np.float32))})()
    out = eng.search({"query": "retry", "top_k": 5, "include_code": True,
                      "format": "json"})
    assert "other-model" in out["code_warning"]
    assert _files(out) == ["howto/retry.md"]


def test_matching_models_keep_the_code_corpus_live(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    eng = SearchEngine.__new__(SearchEngine)
    eng.cfg = cfg

    def fake_load(index_dir):
        if index_dir == cfg.code_index_dir:
            return np.ones((1, 2), dtype=np.float32), CODE_CHUNKS[:1], "same"
        if index_dir == cfg.memory_index_dir:
            return np.ones((1, 2), dtype=np.float32), MEM_CHUNKS, "same"
        return None, [], None

    monkeypatch.setattr(SearchEngine, "_load_one", staticmethod(fake_load))
    counts = eng.reload()
    assert eng.code_warning is None and counts["code"] == 1
