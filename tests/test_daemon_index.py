from singularity.config import Config
from singularity.search import daemon, indexer


class FakeEngine:
    def __init__(self):
        self.reload_calls = 0

    def reload(self):
        self.reload_calls += 1
        return {"memory": 3, "code": 0, "sources": 0}


def test_daemon_update_indexes_and_reloads_in_same_process(tmp_path, monkeypatch):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = FakeEngine()
    calls = []
    monkeypatch.setattr(
        indexer, "update",
        lambda cfg, only_files=None, corpus=None: calls.append((only_files, corpus))
        or {"added": 1})

    out = d._handle({"cmd": "index", "action": "update", "files": ["a.md"]})

    assert calls == [(["a.md"], "memory")]
    assert out["stats"] == {"added": 1}
    assert out["index"] == {"memory": 3, "code": 0, "sources": 0}
    assert out["corpus"] == "memory"
    assert d.engine.reload_calls == 1


def test_daemon_rejects_unknown_index_action(tmp_path):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = FakeEngine()
    out = d._handle({"cmd": "index", "action": "destroy"})
    assert "error" in out


# --- W03: corpus routing ----------------------------------------------------

def test_daemon_routes_sources_corpus(tmp_path, monkeypatch):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = FakeEngine()
    calls = []
    monkeypatch.setattr(
        indexer, "rebuild",
        lambda cfg, corpus=None: calls.append(corpus) or {"files": 2, "chunks": 5})

    out = d._handle({"cmd": "index", "action": "rebuild", "corpus": "sources"})

    assert calls == ["sources"]
    assert out["corpus"] == "sources"
    assert out["stats"] == {"files": 2, "chunks": 5}


def test_daemon_rejects_unknown_corpus(tmp_path):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = FakeEngine()
    out = d._handle({"cmd": "index", "action": "update", "corpus": "nope"})
    assert "unknown corpus" in out["error"]


def test_daemon_refuses_sources_when_disabled(tmp_path):
    """Single gate for every remote path: CLI, nightly and the add catch-up
    all reach indexing through here."""
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={"ingest_index_sources": False}))
    d.engine = FakeEngine()
    out = d._handle({"cmd": "index", "action": "update", "corpus": "sources"})
    assert out["error_type"] == "SourcesIndexDisabled"


def test_daemon_still_indexes_memory_when_sources_disabled(tmp_path, monkeypatch):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={"ingest_index_sources": False}))
    d.engine = FakeEngine()
    monkeypatch.setattr(indexer, "update", lambda cfg, only_files=None, corpus=None:
                        {"added": 0})
    out = d._handle({"cmd": "index", "action": "update"})
    assert out["status"] == "indexed"
