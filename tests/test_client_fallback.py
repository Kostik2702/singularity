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
