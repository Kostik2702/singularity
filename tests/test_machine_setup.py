"""A8 / machine_setup: what this machine has not stated, and when it is said.

The v0.9.0 config is per machine, which turns "a value that is wrong here"
into "a value that is absent here" — quieter, and nothing was asking for it.
"""
from __future__ import annotations

import json

import pytest

from singularity import machine_setup


def test_empty_config_is_entirely_unstated():
    keys = [item["key"] for item in machine_setup.unstated({})]
    assert keys == ["code_repos_dir", "projects", "llm", "imgqa_ml_venv"]
    assert machine_setup.stated({}) == []


def test_an_empty_registry_states_nothing():
    """`{}` is what a half-finished setup leaves behind; counting it as an
    answer is how "configured" and "mentioned once" get confused."""
    data = {"projects": {}, "llm": {}, "code_repos_dir": ""}
    keys = [item["key"] for item in machine_setup.unstated(data)]
    assert "projects" in keys and "llm" in keys and "code_repos_dir" in keys


def test_stated_items_drop_out():
    data = {"code_repos_dir": "/repos",
            "projects": {"p": {"path": "/p"}}}
    keys = [item["key"] for item in machine_setup.unstated(data)]
    assert keys == ["llm", "imgqa_ml_venv"]
    assert machine_setup.stated(data) == ["code_repos_dir", "projects"]


def test_every_unstated_item_carries_the_command_that_states_it():
    for item in machine_setup.unstated({}):
        assert item["how"].startswith("mb ")
        assert item["what"]


def test_summary_is_silent_when_everything_is_stated():
    data = {"code_repos_dir": "/r", "projects": {"p": {}},
            "llm": {"server_bin": "x"}, "imgqa_ml_venv": "/v"}
    assert machine_setup.summary_lines(data) == []


def test_summary_names_the_key_and_the_command():
    lines = machine_setup.summary_lines({"projects": {"p": {}}})
    text = "\n".join(lines)
    assert "code_repos_dir" in text
    assert "mb config set code_repos_dir" in text
    # a key this machine HAS stated is not listed back at it
    assert not [ln for ln in lines if ln.strip().startswith("projects:")]


# --- the two moments it is actually said -------------------------------------

def test_migration_tells_the_arriving_machine_what_it_must_state(tmp_path, monkeypatch, capsys):
    """A vault arrives carrying the settings and none of the local answers.
    The migration is the one moment that is known to be an upgrade."""
    from singularity import config as cfgmod

    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"model": "m"}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    monkeypatch.setattr(cfgmod, "_daemon_running", lambda cfg: False)

    cfgmod.load_config(str(tmp_path))

    err = capsys.readouterr().err
    assert "has not stated" in err
    assert "mb config set code_repos_dir" in err


def test_migration_is_silent_for_a_machine_that_stated_everything(tmp_path, monkeypatch, capsys):
    from singularity import config as cfgmod

    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps({
        "code_repos_dir": "/r", "projects": {"p": {"path": "/p"}},
        "llm": {"server_bin": "x"}, "imgqa_ml_venv": "/v"}), encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    monkeypatch.setattr(cfgmod, "_daemon_running", lambda cfg: False)

    cfgmod.load_config(str(tmp_path))

    assert "has not stated" not in capsys.readouterr().err
