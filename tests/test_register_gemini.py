"""Unit tests for install/register_gemini (GCLI-02): managed GEMINI.md profile +
portable skill links, mirroring the Codex safety contract on the shared primitives.

Covers fresh/append/refresh/malformed markers, foreign-skill preservation, absent
client, old/unparseable version WARN (non-fatal), idempotency, the version parser,
and the missing-repo-skill failure code.
"""
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "register_gemini", ENGINE / "install" / "register_gemini.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


register_gemini = _load()
VAULT = Path("D:/obsidian/memory brain")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME; Gemini 'present' via ~/.gemini with a modern version and no
    real binary on PATH; fixed vault for stable profile bytes."""
    monkeypatch.setattr(register_gemini.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_gemini.register_skills.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_gemini.shutil, "which", lambda _n: None)
    monkeypatch.setattr(register_gemini, "resolve_vault", lambda: VAULT)
    monkeypatch.setattr(register_gemini, "_gemini_version", lambda: (0, 50, 0))
    (tmp_path / ".gemini").mkdir()
    return tmp_path


def _profile(home):
    return home / ".gemini" / "GEMINI.md"


# --- absent client -------------------------------------------------------------

def test_absent_client_skips_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(register_gemini.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_gemini.shutil, "which", lambda _n: None)
    rc = register_gemini.main()
    assert rc == 0
    assert not (tmp_path / ".gemini").exists()


# --- fresh / content -----------------------------------------------------------

def test_fresh_creates_profile_with_block(home):
    rc = register_gemini.main()
    assert rc == 0
    text = _profile(home).read_text(encoding="utf-8")
    assert text.startswith(register_gemini.BEGIN)
    assert text.rstrip().endswith(register_gemini.END)
    assert str(VAULT) in text and "SEM-FIRST" in text
    assert text.endswith("\n")


def test_skills_linked_and_no_council(home):
    register_gemini.main()
    dest = home / ".gemini" / "skills"
    for name in register_gemini.GEMINI_SKILLS:
        assert (dest / name).exists(), name
    for council in ("council", "planning-council", "observation-council"):
        assert council not in register_gemini.GEMINI_SKILLS
        assert not (dest / council).exists()


# --- T02: shared capture-hooks natural-language fragment ----------------------

def test_profile_contains_capture_hooks_fragment(home):
    register_gemini.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "mb hooks status" in text and "mb hooks enable" in text and "mb hooks disable" in text
    assert "stop hooks" in text
    assert register_gemini.CAPTURE_HOOKS_FRAGMENT in text        # verbatim, single source


# --- append / refresh / malformed ---------------------------------------------

def test_append_preserves_user_content(home):
    _profile(home).write_text("# user GEMINI notes\nkeep me\n", encoding="utf-8")
    register_gemini.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "keep me" in text
    assert text.index("keep me") < text.index(register_gemini.BEGIN)


def test_refresh_replaces_only_block(home):
    _profile(home).write_text(
        f"top\n\n{register_gemini.BEGIN}\nSTALE\n{register_gemini.END}\n\nbottom\n",
        encoding="utf-8")
    register_gemini.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "STALE" not in text
    assert "top" in text and "bottom" in text
    assert text.count(register_gemini.BEGIN) == 1


def test_malformed_marker_appends(home):
    _profile(home).write_text(f"user\n{register_gemini.BEGIN}\nhalf\n", encoding="utf-8")
    register_gemini.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "half" in text
    assert text.rstrip().endswith(register_gemini.END)


# --- foreign skill preservation ------------------------------------------------

def test_foreign_skill_left_untouched(home):
    dest = home / ".gemini" / "skills"
    dest.mkdir(parents=True)
    foreign = dest / "user-custom-skill"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("my own skill", encoding="utf-8")
    register_gemini.main()
    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "my own skill"


# --- version handling ----------------------------------------------------------

def test_old_version_warns_but_still_deploys(home, monkeypatch, capsys):
    monkeypatch.setattr(register_gemini, "_gemini_version", lambda: (0, 10, 0))
    rc = register_gemini.main()
    assert rc == 0
    assert _profile(home).exists()                 # profile still applied
    assert "WARN" in capsys.readouterr().err


def test_unparseable_version_warns_but_deploys(home, monkeypatch, capsys):
    monkeypatch.setattr(register_gemini, "_gemini_version", lambda: None)
    rc = register_gemini.main()
    assert rc == 0
    assert _profile(home).exists()
    assert "WARN" in capsys.readouterr().err


# --- idempotency ---------------------------------------------------------------

def test_idempotent_rerun_byte_stable(home):
    register_gemini.main()
    first = _profile(home).read_bytes()
    register_gemini.main()
    assert _profile(home).read_bytes() == first


# --- missing repo skill -> rc 1 ------------------------------------------------

def test_missing_repo_skill_returns_1(home, monkeypatch):
    monkeypatch.setattr(register_gemini, "GEMINI_SKILLS", ["nonexistent-skill-xyz"])
    rc = register_gemini.main()
    assert rc == 1


# --- _gemini_version parser (isolated) -----------------------------------------

def test_version_parser_reads_semver(monkeypatch):
    monkeypatch.setattr(register_gemini.shutil, "which", lambda _n: "/fake/gemini")
    monkeypatch.setattr(register_gemini.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="0.50.0\n", stderr=""))
    assert register_gemini._gemini_version() == (0, 50, 0)


def test_version_parser_none_when_absent(monkeypatch):
    monkeypatch.setattr(register_gemini.shutil, "which", lambda _n: None)
    assert register_gemini._gemini_version() is None


def test_version_parser_none_on_timeout(monkeypatch):
    monkeypatch.setattr(register_gemini.shutil, "which", lambda _n: "/fake/gemini")
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="gemini", timeout=20)
    monkeypatch.setattr(register_gemini.subprocess, "run", boom)
    assert register_gemini._gemini_version() is None


def test_version_parser_none_on_unparseable(monkeypatch):
    monkeypatch.setattr(register_gemini.shutil, "which", lambda _n: "/fake/gemini")
    monkeypatch.setattr(register_gemini.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="dev-build", stderr=""))
    assert register_gemini._gemini_version() is None


# --- GCLI-03: SessionStart hook registration -----------------------------------

def _settings(home):
    return home / ".gemini" / "settings.json"


def _owned_hook_names(home):
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    return [h.get("name")
            for d in data.get("hooks", {}).get("SessionStart", [])
            for h in d.get("hooks", [])]


def test_session_start_hook_registered_at_supported_version(home):
    register_gemini.main()
    assert register_gemini.SESSION_START_HOOK_NAME in _owned_hook_names(home)
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    # SessionStart command points at the gemini adapter, not the Claude one
    assert "gemini_session_start.py" in data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # AfterAgent capture hook is registered too
    aa = [h["name"] for d in data["hooks"]["AfterAgent"] for h in d["hooks"]]
    assert register_gemini.AFTER_AGENT_HOOK_NAME in aa
    assert "gemini_after_agent.py" in data["hooks"]["AfterAgent"][0]["hooks"][0]["command"]


def test_hook_skipped_on_old_version(home, monkeypatch):
    monkeypatch.setattr(register_gemini, "_gemini_version", lambda: (0, 10, 0))
    register_gemini.main()
    assert not _settings(home).exists()          # no hook write on unproven support


def test_hook_skipped_on_unknown_version(home, monkeypatch):
    monkeypatch.setattr(register_gemini, "_gemini_version", lambda: None)
    register_gemini.main()
    assert not _settings(home).exists()


def test_foreign_settings_and_hooks_preserved(home):
    _settings(home).write_text(json.dumps({
        "theme": "dark",
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "x", "name": "user-hook"}]}],
            "BeforeTool": [{"hooks": [{"type": "command", "command": "y", "name": "user-bt"}]}],
        },
    }), encoding="utf-8")
    register_gemini.main()
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert data["theme"] == "dark"                                  # foreign key kept
    assert "user-hook" in _owned_hook_names(home)                   # foreign SessionStart kept
    assert register_gemini.SESSION_START_HOOK_NAME in _owned_hook_names(home)
    assert data["hooks"]["BeforeTool"][0]["hooks"][0]["name"] == "user-bt"  # untouched


def test_malformed_settings_left_untouched(home, capsys):
    broken = "{ this is not valid json"
    _settings(home).write_text(broken, encoding="utf-8")
    register_gemini.main()
    assert _settings(home).read_text(encoding="utf-8") == broken    # not overwritten
    assert "malformed" in capsys.readouterr().err


def test_hook_registration_idempotent(home):
    register_gemini.main()
    register_gemini.main()
    names = _owned_hook_names(home)
    assert names.count(register_gemini.SESSION_START_HOOK_NAME) == 1   # no duplicate


def test_settings_backed_up_before_mutation(home):
    _settings(home).write_text('{"theme":"light"}', encoding="utf-8")
    register_gemini.main()
    backups = list((home / ".gemini").glob("settings.json.bak-*"))
    assert backups, "existing settings.json must be backed up before mutation"


# --- GCLI-05: uninstall (owned-only removal) -----------------------------------

def test_uninstall_removes_profile_keeps_user_text(home):
    _profile(home).write_text("# user top note\nkeep me\n", encoding="utf-8")
    register_gemini.main()                        # appends our block after user text
    res = register_gemini.uninstall()
    assert res["profile"] == "removed"
    text = _profile(home).read_text(encoding="utf-8")
    assert "keep me" in text                      # user text survives
    assert register_gemini.BEGIN not in text      # our block gone


def test_uninstall_removes_owned_hook_keeps_foreign(home):
    _settings(home).write_text(json.dumps({
        "theme": "dark",
        "hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": "x", "name": "user-hook"}]}]},
    }), encoding="utf-8")
    register_gemini.main()                        # adds our SessionStart+AfterAgent hooks
    res = register_gemini.uninstall()
    assert res["hooks"] == 2                       # SessionStart + AfterAgent owned
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert data["theme"] == "dark"                # foreign key kept
    names = [h["name"] for d in data["hooks"]["SessionStart"] for h in d["hooks"]]
    assert names == ["user-hook"]                 # only foreign remains
    assert "AfterAgent" not in data["hooks"]      # our-only event cleaned up
    assert list((home / ".gemini").glob("settings.json.bak-*")), "settings backed up"


def test_uninstall_removes_owned_skill_links_keeps_foreign(home):
    register_gemini.main()                        # 10 owned links
    foreign = home / ".gemini" / "skills" / "user-custom"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("mine", encoding="utf-8")
    res = register_gemini.uninstall()
    assert res["skills_removed"] == len(register_gemini.GEMINI_SKILLS)
    assert (foreign / "SKILL.md").read_text(encoding="utf-8") == "mine"  # foreign untouched


def test_uninstall_keeps_foreign_dir_at_owned_name(home):
    register_gemini.main()
    # user replaced one owned link with their own real directory
    name = register_gemini.GEMINI_SKILLS[0]
    link = home / ".gemini" / "skills" / name
    register_gemini._remove_link(link)
    link.mkdir()
    (link / "user.txt").write_text("do not delete", encoding="utf-8")
    res = register_gemini.uninstall()
    assert res["skills_kept"] >= 1
    assert (link / "user.txt").read_text(encoding="utf-8") == "do not delete"
    assert res["skills_removed"] == len(register_gemini.GEMINI_SKILLS) - 1


def test_uninstall_malformed_settings_untouched(home, capsys):
    broken = "{ not valid json"
    _settings(home).write_text(broken, encoding="utf-8")
    register_gemini.uninstall()
    assert _settings(home).read_text(encoding="utf-8") == broken
    assert "malformed" in capsys.readouterr().err


def test_uninstall_safe_when_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(register_gemini.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_gemini.register_skills.Path, "home",
                        classmethod(lambda cls: tmp_path))
    res = register_gemini.uninstall()             # no ~/.gemini at all
    assert res["profile"] == "absent" and res["hooks"] == 0


def test_install_uninstall_roundtrip_preserves_user_bytes(home):
    _profile(home).write_text("# user GEMINI\nprecious\n", encoding="utf-8")
    _settings(home).write_text('{"theme":"solarized"}', encoding="utf-8")
    register_gemini.main()
    register_gemini.uninstall()
    assert "precious" in _profile(home).read_text(encoding="utf-8")
    assert json.loads(_settings(home).read_text(encoding="utf-8"))["theme"] == "solarized"
