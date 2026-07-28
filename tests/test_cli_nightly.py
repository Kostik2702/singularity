"""T12: `mb nightly` CLI wiring (mirrors test_cli_digest.py)."""
import json

import pytest

from singularity import cli
from singularity.config import Config
from singularity.search import client


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action="update", files=None: {"added": 0})
    return cfg


def test_nightly_json_shape(wired, capsys):
    rc = cli.main(["nightly", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    # W11 added the ingest-cycle steps; ingest_index legitimately reports
    # "skipped" on a vault with no sources, so the status assertion allows it.
    assert set(data["steps"]) == {"index", "ingest_index", "code_index", "digest",
                                  "curator", "gaps", "contradictions", "sources",
                                  "config", "markers", "ingest_log", "trend"}
    assert all(s.get("status") in ("ok", "skipped") for s in data["steps"].values())
    assert data["steps"]["ingest_index"]["status"] == "skipped"
    # C05: no code_repos_dir on this fixture vault -> skipped with a reason,
    # never an error (a corpus nobody switched on is not a failure)
    assert data["steps"]["code_index"]["status"] == "skipped"
    assert "code_repos_dir" in data["steps"]["code_index"]["reason"]


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
