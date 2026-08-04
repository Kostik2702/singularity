"""The daemon must not serve the Config it was born with (issue #12).

Reported symptom: `mb config set code_repos_dir …` wrote the new value,
`mb daemon reload` answered `{"status": "reloaded"}`, and
`mb index rebuild --code` kept reporting `{"files": 0}` indefinitely — while
an in-process rebuild of the very same config indexed 205 files. `reload`
reloaded *indexes* off `self.cfg`, the object captured at daemon start, and
never re-read the config file. The name promised otherwise, and it is the
natural command to reach for after `mb config set`.
"""
import json

import pytest

from singularity.config import Config
from singularity.search import client
from singularity.search.daemon import SearchDaemon


class FakeEngine:
    def __init__(self):
        self.reloads = 0

    def reload(self):
        self.reloads += 1
        return {"memory": 1, "code": 0, "sources": 0}


@pytest.fixture
def daemon(tmp_path):
    cfg = Config(vault=tmp_path, data={"model": "fake",
                                       "code_repos_dir": str(tmp_path / "old")})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    cfg.save()
    d = SearchDaemon(cfg)
    d.engine = FakeEngine()
    return d


def _write_config(cfg, **overrides):
    data = {**json.loads(cfg.config_path.read_text(encoding="utf-8")), **overrides}
    cfg.config_path.write_text(json.dumps(data), encoding="utf-8")


# --- reload actually reloads the config --------------------------------------

def test_reload_picks_up_a_changed_path(daemon, tmp_path):
    new_root = tmp_path / "projects"
    new_root.mkdir()
    _write_config(daemon.cfg, code_repos_dir=str(new_root))

    resp = daemon._handle({"cmd": "reload"})

    assert daemon.cfg.code_repos_dir == new_root      # the derived path moved too
    assert resp["status"] == "reloaded"
    assert resp["config"] == "changed"
    assert "code_repos_dir" in resp["config_changed"]


def test_reload_still_reloads_the_indexes(daemon):
    daemon._handle({"cmd": "reload"})
    assert daemon.engine.reloads == 1


def test_reload_reports_an_unchanged_config_as_such(daemon):
    resp = daemon._handle({"cmd": "reload"})
    assert resp["config"] == "unchanged"
    assert resp["config_changed"] == []


def test_reload_reports_only_key_names_never_values(daemon, tmp_path):
    """The response crosses a socket; config values can be paths or secrets."""
    _write_config(daemon.cfg, code_repos_dir=str(tmp_path / "secret-place"))
    resp = daemon._handle({"cmd": "reload"})
    assert "secret-place" not in json.dumps(resp)


def test_reload_flags_a_model_change_as_needing_a_restart(daemon):
    """The encoder was built at startup from the old model; `reload` cannot
    swap it under a live engine, so saying "reloaded" alone would lie again."""
    _write_config(daemon.cfg, model="some-other-model")
    resp = daemon._handle({"cmd": "reload"})
    assert resp["restart_required"] == ["model"]


# --- a bad config on disk must not take the daemon down ----------------------

def test_a_malformed_config_leaves_the_previous_one_serving(daemon):
    """`load_config()` sys.exit(2)s on a broken file — correct for a CLI, fatal
    for a daemon mid-session."""
    before = dict(daemon.cfg.data)
    daemon.cfg.config_path.write_text("{ not json", encoding="utf-8")

    resp = daemon._handle({"cmd": "reload"})

    assert resp["config"] == "unreadable"
    assert daemon.cfg.data == before
    assert resp["status"] == "reloaded"          # indexes still reloaded


def test_an_invalid_config_is_refused_not_applied(daemon):
    before = dict(daemon.cfg.data)
    daemon.cfg.config_path.write_text(json.dumps({"min_score": "not-a-number"}),
                                      encoding="utf-8")

    resp = daemon._handle({"cmd": "reload"})

    assert resp["config"] == "invalid"
    assert daemon.cfg.data == before


def test_a_config_that_is_not_an_object_is_refused(daemon):
    daemon.cfg.config_path.write_text("[1, 2, 3]", encoding="utf-8")
    assert daemon._handle({"cmd": "reload"})["config"] == "invalid"


def test_a_missing_config_file_is_not_an_error(daemon):
    daemon.cfg.config_path.unlink()
    assert daemon._handle({"cmd": "reload"})["config"] == "absent"


# --- the read-back `mb config set` uses --------------------------------------

def test_config_command_returns_the_live_value(daemon, tmp_path):
    resp = daemon._handle({"cmd": "config", "key": "code_repos_dir"})
    assert resp == {"key": "code_repos_dir", "value": str(tmp_path / "old")}


def test_config_command_rejects_a_non_string_key(daemon):
    assert "error" in daemon._handle({"cmd": "config", "key": {"a": 1}})


def test_config_set_warns_when_the_daemon_holds_another_value(tmp_path, monkeypatch, capsys):
    """The trap: the write succeeds and nothing says the serving process
    ignores it."""
    from singularity import cli

    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(client, "daemon_value",
                        lambda cfg, key: (True, "/old/place"))

    rc = cli.main(["config", "set", "code_repos_dir", "/new/place"])

    err = capsys.readouterr().err
    assert rc == 0
    assert "still serves" in err and "/old/place" in err
    assert "mb daemon reload" in err


def test_config_set_is_quiet_when_the_daemon_agrees(tmp_path, monkeypatch, capsys):
    from singularity import cli

    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(client, "daemon_value",
                        lambda cfg, key: (True, "/new/place"))

    cli.main(["config", "set", "code_repos_dir", "/new/place"])
    assert "still serves" not in capsys.readouterr().err


def test_config_set_is_quiet_when_no_daemon_answers(tmp_path, monkeypatch, capsys):
    """An older daemon, or none at all, must not produce a scary warning."""
    from singularity import cli

    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(client, "daemon_value", lambda cfg, key: (False, None))

    cli.main(["config", "set", "code_repos_dir", "/new/place"])
    assert "still serves" not in capsys.readouterr().err


def test_daemon_value_reports_no_answer_when_the_daemon_is_down(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    assert client.daemon_value(cfg, "model") == (False, None)


# --- `mb daemon restart` ------------------------------------------------------

def test_restart_stops_then_starts(tmp_path, monkeypatch):
    """The actual remedy for this class of problem had no single command."""
    cfg = Config(vault=tmp_path, data={})
    calls = []
    monkeypatch.setattr(client, "stop_daemon", lambda cfg: calls.append("stop") or True)
    monkeypatch.setattr(client, "start_daemon", lambda cfg: calls.append("start") or True)

    assert client.restart_daemon(cfg) is True
    assert calls == ["stop", "start"]


def test_cli_exposes_daemon_restart(tmp_path, monkeypatch, capsys):
    from singularity import cli

    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    monkeypatch.setattr(client, "restart_daemon", lambda cfg: True)

    assert cli.main(["daemon", "restart"]) == 0
    assert "restarted" in capsys.readouterr().out
