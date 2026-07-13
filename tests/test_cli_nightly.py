"""T12: `mb nightly` CLI wiring (mirrors test_cli_digest.py)."""
import json

import pytest

from singularity import cli
from singularity.config import Config
from singularity.search import indexer


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(indexer, "update", lambda cfg, only_files=None: {"added": 0})
    return cfg


def test_nightly_json_shape(wired, capsys):
    rc = cli.main(["nightly", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert set(data["steps"]) == {"index", "digest", "curator", "config", "markers", "trend"}
    assert all(s.get("status") == "ok" for s in data["steps"].values())


def test_nightly_human_output(wired, capsys):
    rc = cli.main(["nightly"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mb nightly" in out
    assert "digest: ok" in out


def test_nightly_exit_code_1_on_step_error(wired, monkeypatch, capsys):
    import singularity.curator as curator_mod
    monkeypatch.setattr(curator_mod, "validate",
                        lambda cfg: (_ for _ in ()).throw(RuntimeError("x")))
    rc = cli.main(["nightly", "--json"])
    assert rc == 1
    data = json.loads(capsys.readouterr().out.strip())
    assert data["steps"]["curator"]["status"] == "error"
