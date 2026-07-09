"""Vault resolution (github#1: bare Windows default leaked onto macOS)."""
import json
from pathlib import Path

from singularity import config
from singularity.config import Config


def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("MB_HOME", raising=False)
    monkeypatch.setattr(config, "POINTER_FILE", tmp_path / "no-pointer.json")
    monkeypatch.setattr(config, "DEFAULT_VAULT_WINDOWS", str(tmp_path / "no-such-vault"))


def test_nothing_configured_returns_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert config.resolve_vault(None) is None


def test_explicit_wins(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert config.resolve_vault(str(tmp_path)) == tmp_path


def test_env_mb_home(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    assert config.resolve_vault(None) == tmp_path


def test_pointer_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    pointer = tmp_path / "pointer.json"
    pointer.write_text('{"vault_path": "%s"}' % str(tmp_path).replace("\\", "\\\\"),
                       encoding="utf-8")
    monkeypatch.setattr(config, "POINTER_FILE", pointer)
    assert config.resolve_vault(None) == tmp_path


def test_default_used_only_if_it_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    existing = tmp_path / "dev-vault"
    existing.mkdir()
    monkeypatch.setattr(config, "DEFAULT_VAULT_WINDOWS", str(existing))
    assert config.resolve_vault(None) == existing


def test_save_is_atomic_no_tmp_left(tmp_path):
    cfg = Config(vault=tmp_path, data={"llm": {"server_port": 1}})
    cfg.save()
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["llm"]["server_port"] == 1
    leftovers = [p for p in cfg.config_path.parent.iterdir()
                 if p.name.startswith("config") and p.suffix != ".json"]
    assert leftovers == []
