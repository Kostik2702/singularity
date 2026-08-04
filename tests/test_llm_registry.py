"""CLI-01: `mb llm add` / `mb llm remove` — the write half `list` never had.

Until v0.7.0 the model registry was read-only, and `mb llm list` told people to
"add to llm.models in .mb/config.json" by hand — the exact edit the vault guard
flags, in the file `mb config set` owns. The other end was worse: `doctor
A6-config-paths` reports dead `llm.models.*.gguf` paths after a move between
machines, and there was no command to repoint or drop one. `mb project` has had
`add`/`remove` since T03; models had neither.
"""
import json

import pytest

from singularity import cli, llm
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    # daemon_value would try a socket; the advisory path is not under test here
    monkeypatch.setattr(llm, "_warn_daemon_stale", lambda _cfg: None)
    return cfg


def _models(cfg):
    return json.loads(
        (cfg.vault / ".mb" / "config.json").read_text(encoding="utf-8")
    )["llm"]["models"]


# --- add ---------------------------------------------------------------------

def test_add_registers_a_model(wired, cfg, tmp_path):
    gguf = tmp_path / "q.gguf"
    gguf.write_bytes(b"")
    assert cli.main(["llm", "add", "q9b", str(gguf)]) == 0
    assert _models(cfg)["q9b"]["gguf"] == str(gguf)


def test_add_accepts_extra_server_args(wired, cfg, tmp_path):
    gguf = tmp_path / "q.gguf"
    gguf.write_bytes(b"")
    assert cli.main(["llm", "add", "q9b", str(gguf),
                     "--", "-ngl", "99", "--jinja"]) == 0
    assert _models(cfg)["q9b"]["args"] == ["-ngl", "99", "--jinja"]


def test_add_registers_a_path_that_does_not_exist_yet(wired, cfg, capsys):
    """A config is often prepared before the weights are downloaded, and A6
    already reports paths that resolve nowhere. Say so; do not refuse."""
    assert cli.main(["llm", "add", "q9b", "/models/not-yet.gguf"]) == 0
    assert _models(cfg)["q9b"]["gguf"] == "/models/not-yet.gguf"
    assert "does not exist on this machine yet" in capsys.readouterr().err


def test_add_twice_updates_and_keeps_untouched_fields(wired, cfg, capsys):
    cli.main(["llm", "add", "q9b", "/a.gguf", "--", "-ngl", "99"])
    capsys.readouterr()
    assert cli.main(["llm", "add", "q9b", "/b.gguf"]) == 0
    spec = _models(cfg)["q9b"]
    assert spec["gguf"] == "/b.gguf"
    assert spec["args"] == ["-ngl", "99"], "repointing must not drop server args"
    assert "model updated" in capsys.readouterr().out


def test_add_preserves_the_rest_of_the_config(wired, cfg):
    cfg.data["model"] = "custom"
    cfg.data["llm"] = {"server_bin": "/llama-server"}
    cli.main(["llm", "add", "q9b", "/a.gguf"])
    on_disk = json.loads((cfg.vault / ".mb" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["model"] == "custom"
    assert on_disk["llm"]["server_bin"] == "/llama-server"


def test_add_rejects_an_empty_name(wired, cfg, capsys):
    assert cli.main(["llm", "add", "  ", "/a.gguf"]) == 1
    assert "must not be empty" in capsys.readouterr().err


# --- remove ------------------------------------------------------------------

def test_remove_unregisters(wired, cfg):
    cli.main(["llm", "add", "q9b", "/a.gguf"])
    assert cli.main(["llm", "remove", "q9b"]) == 0
    assert "q9b" not in _models(cfg)


def test_remove_unknown_names_what_is_registered(wired, cfg, capsys):
    cli.main(["llm", "add", "q9b", "/a.gguf"])
    capsys.readouterr()
    assert cli.main(["llm", "remove", "ghost"]) == 1
    assert "unknown model: ghost" in capsys.readouterr().err
    assert "q9b" in _models(cfg)


def test_remove_refuses_while_the_model_is_being_served(wired, cfg, capsys):
    """Stopping someone's running server as a side effect of a registry edit is
    not a config change, it is an outage."""
    cli.main(["llm", "add", "q9b", "/a.gguf"])
    llm.write_state(cfg, {"model": "q9b", "server_pid": 4242})
    capsys.readouterr()
    assert cli.main(["llm", "remove", "q9b"]) == 1
    err = capsys.readouterr().err
    assert "currently being served" in err and "mb llm stop" in err
    assert "q9b" in _models(cfg), "nothing removed"


def test_remove_force_unregisters_and_says_the_server_lives_on(wired, cfg, capsys):
    cli.main(["llm", "add", "q9b", "/a.gguf"])
    llm.write_state(cfg, {"model": "q9b", "server_pid": 4242})
    capsys.readouterr()
    assert cli.main(["llm", "remove", "q9b", "--force"]) == 0
    assert "q9b" not in _models(cfg)
    assert "still running" in capsys.readouterr().err


def test_remove_of_an_idle_model_needs_no_force(wired, cfg):
    cli.main(["llm", "add", "q9b", "/a.gguf"])
    cli.main(["llm", "add", "other", "/b.gguf"])
    llm.write_state(cfg, {"model": "other", "server_pid": 4242})
    assert cli.main(["llm", "remove", "q9b"]) == 0


# --- the advice `list` gives -------------------------------------------------

def test_empty_registry_points_at_the_command_not_the_file(wired, cfg, capsys):
    cfg.data["llm"] = {"server_bin": "/llama-server"}
    cli.main(["llm", "list"])
    out = capsys.readouterr().out
    assert "mb llm add" in out
    assert ".mb/config.json" not in out, "the CLI must not send people to edit the file"
