"""Machine-specific paths in a vault that is designed to travel (issue #14).

A vault cloned onto a new machine brings `.mb/config.json` with it, including
every absolute path the previous machine wrote. `mb doctor` validated exactly
one of them and reported `PASS 31 | WARN 1` while local-LLM delegation and four
project registrations were equally dead.
"""
import os

import pytest

from singularity import doctor
from singularity.machine import config_paths
from singularity.config import Config

# The reported config, verbatim in shape: forward slashes under `llm`, escaped
# backslashes under `projects`. Any migration helper has to handle both.
WINDOWS_CONFIG = {
    "code_repos_dir": "E:/Project",
    "llm": {
        "server_bin": "E:/llama.cpp/llama-server.exe",
        "models": {
            "qwythos9b": {
                "gguf": "E:/models/Qwythos-9B-Q4_K_M.gguf",
                "args": ["--chat-template-file", "E:/models/qwythos9b.chat.jinja",
                         "-ngl", "99", "--host", "127.0.0.1"],
            },
            "qwen27b": {"gguf": "E:/models/ThinkingCap-Qwen3.6-27B-Q4_K_M.gguf"},
        },
    },
    "projects": {
        "storyforge": {"path": "D:\\StoryForge"},
        "singularity": {"path": "E:\\Singularity"},
    },
}


# --- enumeration -------------------------------------------------------------

def test_every_path_valued_key_is_found():
    keys = [k for k, _v in config_paths.path_entries(WINDOWS_CONFIG)]
    assert keys == [
        "code_repos_dir",
        "llm.server_bin",
        "llm.models.qwythos9b.gguf",
        "llm.models.qwythos9b.args[1]",
        "llm.models.qwen27b.gguf",
        "projects.storyforge.path",
        "projects.singularity.path",
    ]


def test_ordinary_flags_and_numbers_are_not_paths():
    """`--host`, `127.0.0.1`, `-ngl`, `99` sit in the same list as the one real
    path; reporting them as missing files would drown the signal."""
    entries = dict(config_paths.path_entries(WINDOWS_CONFIG))
    assert entries["llm.models.qwythos9b.args[1]"].endswith(".jinja")
    assert not any(k.startswith("llm.models.qwythos9b.args[") and k != "llm.models.qwythos9b.args[1]"
                   for k in entries)


def test_vault_relative_keys_are_never_treated_as_paths():
    """`skip_path_parts`, `immutable_roots` and `personal_roots` are relative by
    design — a heuristic sweep would report them as missing absolute paths."""
    data = {"skip_path_parts": ["learnings/archive"],
            "immutable_roots": [".mb/sources"],
            "personal_roots": ["diary"],
            "skip_dirs": [".obsidian", ".mb"]}
    assert config_paths.path_entries(data) == []


def test_empty_and_absent_values_are_skipped():
    assert config_paths.path_entries({"code_repos_dir": None}) == []
    assert config_paths.path_entries({"code_repos_dir": "  "}) == []
    assert config_paths.path_entries({"llm": {"models": {"m": "not-an-object"}}}) == []


# --- foreign-OS detection ----------------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="POSIX host view")
def test_windows_paths_are_foreign_on_posix():
    assert config_paths.foreign_os("E:/Project") == "windows"
    assert config_paths.foreign_os("D:\\StoryForge") == "windows"
    assert config_paths.foreign_os("/Users/me/projects") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows host view")
def test_posix_paths_are_foreign_on_windows():
    assert config_paths.foreign_os("/Users/me/projects") == "posix"
    assert config_paths.foreign_os("E:/Project") is None


def test_relative_paths_belong_to_no_os():
    assert config_paths.foreign_os("some/relative/path") is None
    assert config_paths.foreign_os("~/code") is None


# --- doctor ------------------------------------------------------------------

def _check(cfg):
    rep = doctor.Report(quiet=True)
    doctor.config_paths_block(rep, cfg)
    return next(c for c in rep.checks if c["id"] == "A6-config-paths")


def test_doctor_states_the_cross_os_case_once(tmp_path, monkeypatch):
    """Four dead keys should read as one understandable sentence, not four
    confusing absences."""
    monkeypatch.setattr(config_paths, "foreign_os", lambda v: "windows")
    cfg = Config(vault=tmp_path, data=dict(WINDOWS_CONFIG))
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)

    check = _check(cfg)

    assert check["status"] == "WARN"
    assert "another operating system" in check["detail"]
    assert "llm.server_bin" in check["detail"]
    assert "projects.storyforge.path" in check["detail"]
    assert "llm.models.qwythos9b.args[1]" in check["detail"]
    assert "cannot be guessed" in check["fix"]


def test_doctor_reports_a_merely_missing_path(tmp_path):
    """Same-OS but gone: a typo or an unmounted drive, not a migration."""
    cfg = Config(vault=tmp_path,
                 data={"code_repos_dir": str(tmp_path / "not-here")})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)

    check = _check(cfg)

    assert check["status"] == "WARN"
    assert "do not exist on this machine" in check["detail"]
    assert "code_repos_dir" in check["detail"]


def test_doctor_passes_when_every_path_resolves(tmp_path):
    real = tmp_path / "repos"
    real.mkdir()
    cfg = Config(vault=tmp_path, data={"code_repos_dir": str(real)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)

    assert _check(cfg)["status"] == "PASS"


def test_doctor_skips_a_config_without_absolute_paths(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    assert _check(cfg)["status"] == "SKIP"
