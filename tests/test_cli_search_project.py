"""T04: `mb search --project` selector expansion (group/id/reserved) via the
single resolve_selector reuse point — client.search() is stubbed so this file
tests only the CLI-level expansion/error contract, not the search engine."""
import pytest

from singularity import cli
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={"projects": {
        "singularity": {"path": "D:/Singularity", "group": "core"},
        "other": {"path": "D:/other", "group": "core"},
        "solo": {"path": "D:/solo"},
    }})


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


def _stub_client(monkeypatch, seen):
    from singularity.search import client
    monkeypatch.setattr(client, "search",
                        lambda cfg, payload: seen.update(payload=payload) or {"lines": []})


def test_single_id_expands_to_itself(wired, monkeypatch):
    seen = {}
    _stub_client(monkeypatch, seen)
    cli.main(["search", "q", "--project", "singularity"])
    assert seen["payload"]["filter_project"] == ["singularity"]


def test_group_expands_to_all_members(wired, monkeypatch):
    seen = {}
    _stub_client(monkeypatch, seen)
    cli.main(["search", "q", "--project", "core"])
    assert seen["payload"]["filter_project"] == ["other", "singularity"]


def test_multiple_comma_tokens_union(wired, monkeypatch):
    seen = {}
    _stub_client(monkeypatch, seen)
    cli.main(["search", "q", "--project", "solo,general"])
    assert seen["payload"]["filter_project"] == ["general", "solo"]


def test_reserved_tokens_pass_through(wired, monkeypatch):
    seen = {}
    _stub_client(monkeypatch, seen)
    cli.main(["search", "q", "--project", "personal"])
    assert seen["payload"]["filter_project"] == ["personal"]


def test_no_project_flag_leaves_filter_none(wired, monkeypatch):
    seen = {}
    _stub_client(monkeypatch, seen)
    cli.main(["search", "q"])
    assert seen["payload"]["filter_project"] is None


def test_unknown_token_errors_before_calling_client(wired, monkeypatch, capsys):
    called = []
    from singularity.search import client
    monkeypatch.setattr(client, "search", lambda cfg, payload: called.append(1))
    rc = cli.main(["search", "q", "--project", "nope"])
    assert rc == 1
    assert not called
    err = capsys.readouterr().err
    assert "unknown project/group 'nope'" in err
