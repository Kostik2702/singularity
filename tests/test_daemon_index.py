from singularity.config import Config
from singularity.search import daemon, indexer


class FakeEngine:
    def __init__(self):
        self.reload_calls = 0

    def reload(self):
        self.reload_calls += 1
        return {"memory": 3, "code": 0}


def test_daemon_update_indexes_and_reloads_in_same_process(tmp_path, monkeypatch):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = FakeEngine()
    calls = []
    monkeypatch.setattr(indexer, "update",
                        lambda cfg, only_files=None: calls.append(only_files) or {"added": 1})

    out = d._handle({"cmd": "index", "action": "update", "files": ["a.md"]})

    assert calls == [["a.md"]]
    assert out["stats"] == {"added": 1}
    assert out["index"] == {"memory": 3, "code": 0}
    assert d.engine.reload_calls == 1


def test_daemon_rejects_unknown_index_action(tmp_path):
    d = daemon.SearchDaemon(Config(vault=tmp_path, data={}))
    d.engine = FakeEngine()
    out = d._handle({"cmd": "index", "action": "destroy"})
    assert "error" in out
