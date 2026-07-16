"""AUD-15: client warm/cold search paths (no real daemon, no model)."""
from singularity.config import Config
from singularity.search import client


def _cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def test_warm_search_uses_daemon(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_read_port", lambda cfg: 5000)
    monkeypatch.setattr(client, "_request",
                        lambda port, payload, **k: {"results": ["hit"], "warm": True})
    out = client.search(_cfg(tmp_path), {"cmd": "search", "query": "x"})
    assert out["warm"] is True


def test_cold_fallback_when_daemon_absent_and_start_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "_read_port", lambda cfg: None)
    monkeypatch.setattr(client, "start_daemon", lambda cfg: False)
    monkeypatch.setattr(client, "_cold_search",
                        lambda cfg, payload: {"results": [], "cold": True})
    out = client.search(_cfg(tmp_path), {"cmd": "search", "query": "x"})
    assert out["cold"] is True


def test_start_then_warm(tmp_path, monkeypatch):
    ports = iter([None, 6000])
    monkeypatch.setattr(client, "_read_port", lambda cfg: next(ports, 6000))
    monkeypatch.setattr(client, "start_daemon", lambda cfg: True)
    monkeypatch.setattr(client, "_request",
                        lambda port, payload, **k: {"results": ["hit"], "started": True})
    out = client.search(_cfg(tmp_path), {"cmd": "search", "query": "x"})
    assert out["started"] is True


def test_run_index_uses_daemon_with_long_read_timeout(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "_read_port", lambda cfg: 7000)

    def fake_request(port, payload, **kwargs):
        seen.update(port=port, payload=payload, kwargs=kwargs)
        return {"status": "indexed", "stats": {"added": 1}}

    monkeypatch.setattr(client, "_request", fake_request)
    stats = client.run_index(_cfg(tmp_path), "update", ["a.md"])
    assert stats == {"added": 1}
    assert seen["payload"] == {"cmd": "index", "action": "update", "files": ["a.md"]}
    assert seen["kwargs"]["read_timeout"] >= 300


def test_run_index_starts_daemon_instead_of_local_fallback(tmp_path, monkeypatch):
    ports = iter([None, 7100])
    monkeypatch.setattr(client, "_read_port", lambda cfg: next(ports, 7100))
    monkeypatch.setattr(client, "start_daemon", lambda cfg: True)
    monkeypatch.setattr(client, "_request",
                        lambda port, payload, **kwargs:
                        {"status": "indexed", "stats": {"added": 0}})
    assert client.run_index(_cfg(tmp_path), "update") == {"added": 0}


def test_run_index_recovers_from_stale_port(tmp_path, monkeypatch):
    ports = iter([7200, 7300])
    calls = []
    monkeypatch.setattr(client, "_read_port", lambda cfg: next(ports, 7300))
    monkeypatch.setattr(client, "start_daemon",
                        lambda cfg: calls.append("start") or True)

    def fake_request(port, payload, **kwargs):
        calls.append(port)
        if port == 7200:
            return None
        return {"status": "indexed", "stats": {"added": 2}}

    monkeypatch.setattr(client, "_request", fake_request)
    assert client.run_index(_cfg(tmp_path), "update") == {"added": 2}
    assert calls == [7200, "start", 7300]
