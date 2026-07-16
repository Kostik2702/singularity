from types import SimpleNamespace

from singularity import cli
from singularity.config import Config


def test_model_status_returns_failure_when_missing(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={})
    from singularity.search import model_store
    monkeypatch.setattr(model_store, "status",
                        lambda cfg: {"installed": False, "reason": "manifest missing"})
    rc = cli.cmd_model(cfg, SimpleNamespace(model_action="status", json=False,
                                             revision=None, force=False))
    assert rc == 1
    assert "manifest missing" in capsys.readouterr().out


def test_model_install_restarts_daemon(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    from singularity.search import client, model_store
    calls = []
    monkeypatch.setattr(model_store, "install",
                        lambda cfg, revision=None, force=False:
                        calls.append("install") or {"installed": True})
    monkeypatch.setattr(client, "stop_daemon",
                        lambda cfg: calls.append("stop") or True)
    monkeypatch.setattr(client, "start_daemon",
                        lambda cfg: calls.append("start") or True)
    rc = cli.cmd_model(cfg, SimpleNamespace(model_action="install", json=True,
                                             revision=None, force=False))
    assert rc == 0
    assert calls == ["install", "stop", "start"]
