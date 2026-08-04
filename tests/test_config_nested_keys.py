"""`mb config set` speaks the dotted notation `mb doctor` prints.

Reported symptom (2026-08-04, on the v0.6.0 install): `doctor A6-config-paths`
listed `projects.singularity.path` among the paths belonging to another OS and
printed `fix: mb config set <key> "<path>"`. Running exactly that fix reported
success — and so did `mb daemon reload` (`config_changed`) and `mb config
validate` — while the nested value never moved. What landed on disk was a
literal top-level key `"projects.singularity.path"`, ignored by every reader,
and the WARN came back unchanged on the next `mb doctor`.

Three confirmations of a write that did not happen is worse than a plain
failure, so these tests pin both halves: the dotted write reaches the nested
value, and a path that does not exist fails loudly instead of inventing a key.
"""
import json

import pytest

from singularity import cli
from singularity.config import (
    Config,
    ConfigKeyError,
    get_config_key,
    parse_config_key,
    set_config_key,
)


@pytest.fixture
def data():
    return {
        "model": "flat-value",
        "projects": {
            "singularity": {"path": r"E:\Singularity", "group": "tooling",
                            "about": "memory engine"},
        },
        "llm": {
            "server_bin": "E:/llama.cpp/llama-server.exe",
            "models": {"q9b": {"gguf": "E:/models/q.gguf",
                               "args": ["-ngl", "99", "--chat-template-file",
                                        "E:/templates/q.jinja"]}},
        },
    }


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


# --- key parsing -------------------------------------------------------------

def test_parse_plain_and_indexed_segments():
    assert parse_config_key("code_repos_dir") == ["code_repos_dir"]
    assert parse_config_key("projects.singularity.path") == [
        "projects", "singularity", "path"]
    # The `args[14]` form comes from config_paths.path_entries verbatim.
    assert parse_config_key("llm.models.q9b.args[14]") == [
        "llm", "models", "q9b", "args", 14]


def test_empty_segment_is_rejected():
    with pytest.raises(ConfigKeyError):
        parse_config_key("projects..path")


# --- writing -----------------------------------------------------------------

def test_dotted_write_lands_in_the_nested_value(data):
    out = set_config_key(data, "projects.singularity.path", "/Users/k/projects/s")
    assert out["projects"]["singularity"]["path"] == "/Users/k/projects/s"
    assert "projects.singularity.path" not in out, "no literal top-level key"


def test_dotted_write_keeps_the_siblings(data):
    out = set_config_key(data, "projects.singularity.path", "/new")
    assert out["projects"]["singularity"]["group"] == "tooling"
    assert out["projects"]["singularity"]["about"] == "memory engine"


def test_dotted_write_does_not_mutate_the_input(data):
    set_config_key(data, "projects.singularity.path", "/new")
    assert data["projects"]["singularity"]["path"] == r"E:\Singularity"


def test_indexed_write_into_a_list(data):
    out = set_config_key(data, "llm.models.q9b.args[3]", "/Users/k/templates/q.jinja")
    assert out["llm"]["models"]["q9b"]["args"][3] == "/Users/k/templates/q.jinja"
    assert out["llm"]["models"]["q9b"]["args"][:3] == ["-ngl", "99",
                                                      "--chat-template-file"]


def test_flat_key_still_writes_flat(data):
    assert set_config_key(data, "model", "other")["model"] == "other"


def test_new_flat_key_is_still_allowed(data):
    """Unknown top-level keys stay writable — connectors and forward-compatible
    settings live there; only the CLI now warns about them."""
    assert set_config_key(data, "future_key", 1)["future_key"] == 1


def test_a_literal_dotted_key_already_present_wins(data):
    """A config that somehow carries a dotted literal key keeps answering for
    it, so this change cannot orphan a value written by an older build."""
    data["projects.singularity.path"] = "legacy"
    out = set_config_key(data, "projects.singularity.path", "rewritten")
    assert out["projects.singularity.path"] == "rewritten"
    assert out["projects"]["singularity"]["path"] == r"E:\Singularity"


# --- refusing to invent structure -------------------------------------------

def test_unknown_project_is_an_error_not_a_new_key(data):
    with pytest.raises(ConfigKeyError) as exc:
        set_config_key(data, "projects.ghost.path", "/x")
    assert "mb project add" in str(exc.value)


def test_unknown_leaf_names_what_is_there(data):
    with pytest.raises(ConfigKeyError) as exc:
        set_config_key(data, "projects.singularity.colour", "red")
    assert "group" in str(exc.value) and "path" in str(exc.value)


def test_index_out_of_range_is_an_error(data):
    with pytest.raises(ConfigKeyError):
        set_config_key(data, "llm.models.q9b.args[99]", "/x")


def test_walking_through_a_scalar_is_an_error(data):
    with pytest.raises(ConfigKeyError):
        set_config_key(data, "model.nested.key", "x")


# --- reading -----------------------------------------------------------------

def test_get_config_key_reads_nested_and_indexed(data):
    assert get_config_key(data, "projects.singularity.path") == r"E:\Singularity"
    assert get_config_key(data, "llm.models.q9b.args[2]") == "--chat-template-file"


def test_config_get_answers_for_dotted_keys(cfg, data):
    cfg.data = data
    assert cfg.get("projects.singularity.path") == r"E:\Singularity"
    assert cfg.get("projects.ghost.path", "fallback") == "fallback"


def test_config_get_flat_lookup_is_unchanged(cfg):
    cfg.data = {"model": "m"}
    assert cfg.get("model") == "m"
    assert cfg.get("missing") is None
    assert cfg.get("missing", 7) == 7


# --- CLI ---------------------------------------------------------------------

def test_cli_set_writes_the_nested_value_to_disk(wired, cfg, data, capsys):
    cfg.data = dict(data)
    assert cli.main(["config", "set", "projects.singularity.path", "/Users/k/s"]) == 0
    on_disk = json.loads((cfg.vault / ".mb" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["projects"]["singularity"]["path"] == "/Users/k/s"
    assert "projects.singularity.path" not in on_disk


def test_cli_set_fails_loudly_for_a_section_that_does_not_exist(wired, cfg, data,
                                                               capsys):
    cfg.data = dict(data)
    assert cli.main(["config", "set", "projects.ghost.path", "/x"]) == 1
    assert "no such config section" in capsys.readouterr().err
    assert not (cfg.vault / ".mb" / "config.json").exists(), "nothing written"


def test_cli_set_refuses_an_unknown_flat_key(wired, cfg, capsys):
    """Owner decision 2026-08-04 (CLI-02): nothing reads an unknown key, so
    writing one is a no-op the user reads as a fix. It warned until v0.6.4."""
    assert cli.main(["config", "set", "modle", "typo"]) == 1
    err = capsys.readouterr().err
    assert "unknown config key" in err
    assert "did you mean: model?" in err, "a one-keystroke typo deserves the hint"
    assert not (cfg.vault / ".mb" / "config.json").exists(), "nothing written"


def test_cli_set_allow_unknown_writes_it(wired, cfg, capsys):
    """The legitimate case — a connector key, or one a newer build reads —
    stays reachable, just explicit."""
    assert cli.main(["config", "set", "my_future_key", "1", "--allow-unknown"]) == 0
    on_disk = json.loads((cfg.vault / ".mb" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["my_future_key"] == 1
    assert "written as requested" in capsys.readouterr().err


def test_cli_set_known_key_needs_no_flag(wired, cfg, capsys):
    assert cli.main(["config", "set", "excerpt_chars", "200"]) == 0
    assert "unknown config key" not in capsys.readouterr().err


def test_cli_get_reads_a_dotted_key(wired, cfg, data, capsys):
    cfg.data = dict(data)
    assert cli.main(["config", "get", "projects.singularity.path"]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == r"E:\Singularity"


# --- the neighbouring loss: `project add` used to drop group/about -----------

def test_project_add_repoints_without_dropping_metadata(wired, cfg, capsys):
    cli.main(["project", "add", "s", "E:/S", "--group", "tooling",
              "--about", "memory engine"])
    capsys.readouterr()
    assert cli.main(["project", "add", "s", "/Users/k/projects/s"]) == 0
    entry = cfg.data["projects"]["s"]
    assert entry["path"] == "/Users/k/projects/s"
    assert entry["group"] == "tooling"
    assert entry["about"] == "memory engine"
    assert "project updated" in capsys.readouterr().out
