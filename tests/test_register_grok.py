"""Unit tests for install/register_grok (GRK-02): managed AGENTS.md profile +
portable skill links into $GROK_HOME (or ~/.grok), on the shared primitives.

Covers absent client, GROK_HOME override vs home fallback (same resolver for
install AND uninstall), version probe success/timeout/unparseable, first
install, idempotent refresh, malformed-marker repair, user-text preservation,
missing engine skill, foreign-skill conflict backup, exact-link uninstall,
foreign-at-owned-name preservation, and the install->uninstall byte round-trip.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "register_grok", ENGINE / "install" / "register_grok.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


register_grok = _load()
VAULT = Path("D:/obsidian/memory brain")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated home-fallback root: GROK_HOME unset, Path.home() -> tmp_path,
    Grok 'present' via ~/.grok with the pinned contract version and no real
    binary on PATH; fixed vault for stable profile bytes."""
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(register_grok.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: None)
    monkeypatch.setattr(register_grok, "resolve_vault", lambda: VAULT)
    monkeypatch.setattr(register_grok, "_grok_version", lambda: (0, 2, 111))
    (tmp_path / ".grok").mkdir()
    return tmp_path


def _profile(home):
    return home / ".grok" / "AGENTS.md"


# --- absent client -------------------------------------------------------------

def test_absent_client_skips_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(register_grok.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: None)
    rc = register_grok.main()
    assert rc == 0
    assert not (tmp_path / ".grok").exists()


# --- config root resolver -------------------------------------------------------

def test_grok_home_env_overrides_home_dir(tmp_path, monkeypatch):
    """GROK_HOME wins over ~/.grok — Grok's own resolution order (GRK-00)."""
    custom = tmp_path / "custom-grok-root"
    custom.mkdir()
    monkeypatch.setenv("GROK_HOME", str(custom))
    monkeypatch.setattr(register_grok.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: None)
    monkeypatch.setattr(register_grok, "resolve_vault", lambda: VAULT)
    monkeypatch.setattr(register_grok, "_grok_version", lambda: (0, 2, 111))
    rc = register_grok.main()
    assert rc == 0
    assert (custom / "AGENTS.md").exists()
    assert not (tmp_path / ".grok").exists(), "home fallback must not be touched"


def test_home_fallback_when_grok_home_unset(home):
    rc = register_grok.main()
    assert rc == 0
    assert _profile(home).exists()


def test_uninstall_uses_same_resolver(tmp_path, monkeypatch):
    """Install under GROK_HOME, uninstall under the SAME GROK_HOME — the home
    fallback path must stay untouched in both directions."""
    custom = tmp_path / "custom-grok-root"
    custom.mkdir()
    monkeypatch.setenv("GROK_HOME", str(custom))
    monkeypatch.setattr(register_grok.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: None)
    monkeypatch.setattr(register_grok, "resolve_vault", lambda: VAULT)
    monkeypatch.setattr(register_grok, "_grok_version", lambda: (0, 2, 111))
    register_grok.main()
    result = register_grok.uninstall()
    assert result["profile"] == "removed"
    assert not (custom / "AGENTS.md").exists()


# --- version probe ---------------------------------------------------------------

def test_version_parses_observed_format(monkeypatch):
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: "/usr/bin/grok")
    monkeypatch.setattr(register_grok.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="grok 0.2.111 (94172f2aa4)\n", stderr=""))
    assert register_grok._grok_version() == (0, 2, 111)


def test_version_none_when_binary_absent(monkeypatch):
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: None)
    assert register_grok._grok_version() is None


def test_version_none_on_timeout(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="grok", timeout=30)
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: "/usr/bin/grok")
    monkeypatch.setattr(register_grok.subprocess, "run", boom)
    assert register_grok._grok_version() is None


def test_version_none_when_unparseable(monkeypatch):
    monkeypatch.setattr(register_grok.shutil, "which", lambda _n: "/usr/bin/grok")
    monkeypatch.setattr(register_grok.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(
                            a, 0, stdout="not a version at all\n", stderr=""))
    assert register_grok._grok_version() is None


def test_old_version_warns_but_installs(home, monkeypatch, capsys):
    monkeypatch.setattr(register_grok, "_grok_version", lambda: (0, 1, 0))
    rc = register_grok.main()
    assert rc == 0
    assert _profile(home).exists()
    assert "WARN" in capsys.readouterr().err


def test_unprobeable_version_warns_but_installs(home, monkeypatch, capsys):
    monkeypatch.setattr(register_grok, "_grok_version", lambda: None)
    rc = register_grok.main()
    assert rc == 0
    assert _profile(home).exists()
    assert "WARN" in capsys.readouterr().err


# --- fresh install / content ----------------------------------------------------

def test_fresh_creates_profile_with_block(home):
    rc = register_grok.main()
    assert rc == 0
    text = _profile(home).read_text(encoding="utf-8")
    assert text.startswith(register_grok.BEGIN)
    assert text.rstrip().endswith(register_grok.END)
    assert str(VAULT) in text and "SEM-FIRST" in text
    assert text.endswith("\n")


def test_profile_is_bom_less_utf8(home):
    """GRK-00: a BOM in AGENTS.md leaks into the model context as mojibake."""
    register_grok.main()
    raw = _profile(home).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")


def test_profile_contains_capture_hooks_fragment(home):
    register_grok.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert register_grok.CAPTURE_HOOKS_FRAGMENT in text        # verbatim, single source


def test_profile_contains_static_recall_instruction(home):
    """GRK-04 dynamic recall is NO-GO (GRK-00): the profile itself must carry
    the session-start recall instruction."""
    register_grok.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "mb search" in text
    assert "session start" in text.lower()


def test_skills_linked_and_no_council(home):
    register_grok.main()
    dest = home / ".grok" / "skills"
    for name in register_grok.GROK_SKILLS:
        assert (dest / name).exists(), name
    for council in ("council", "planning-council", "observation-council"):
        assert council not in register_grok.GROK_SKILLS
        assert not (dest / council).exists()


def test_skill_list_in_profile_matches_manifest(home):
    register_grok.main()
    text = _profile(home).read_text(encoding="utf-8")
    for name in register_grok.GROK_SKILLS:
        assert name in text


# --- idempotency / refresh / malformed markers -----------------------------------

def test_second_run_is_idempotent(home):
    register_grok.main()
    first = _profile(home).read_bytes()
    rc = register_grok.main()
    assert rc == 0
    assert _profile(home).read_bytes() == first


def test_append_preserves_user_content(home):
    _profile(home).parent.mkdir(parents=True, exist_ok=True)
    _profile(home).write_text("# user AGENTS notes\nkeep me\n", encoding="utf-8")
    register_grok.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "keep me" in text
    assert text.index("keep me") < text.index(register_grok.BEGIN)


def test_refresh_rewrites_only_managed_block(home):
    _profile(home).parent.mkdir(parents=True, exist_ok=True)
    _profile(home).write_text("before\n", encoding="utf-8")
    register_grok.main()
    stale = _profile(home).read_text(encoding="utf-8").replace(
        "SEM-FIRST", "STALE-MARKER")
    _profile(home).write_text(stale + "\nafter user note\n", encoding="utf-8")
    register_grok.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "STALE-MARKER" not in text
    assert "before" in text and "after user note" in text


def test_malformed_block_appends_instead_of_bad_splice(home):
    """Only one marker present -> merge_managed_block appends a fresh block and
    never splices; the damaged text stays as user bytes."""
    _profile(home).parent.mkdir(parents=True, exist_ok=True)
    _profile(home).write_text(
        f"user top\n{register_grok.BEGIN}\nhalf-written, END marker lost\n",
        encoding="utf-8")
    register_grok.main()
    text = _profile(home).read_text(encoding="utf-8")
    assert "user top" in text and "half-written, END marker lost" in text
    assert text.count(register_grok.BEGIN) == 2      # damaged + fresh managed block
    assert text.count(register_grok.END) == 1


# --- skills: conflicts and failures ----------------------------------------------

def test_missing_engine_skill_returns_rc1(home, monkeypatch):
    monkeypatch.setattr(register_grok, "GROK_SKILLS",
                        register_grok.GROK_SKILLS + ["skill-that-does-not-exist"])
    rc = register_grok.main()
    assert rc == 1


def test_foreign_skill_conflict_backed_up_not_deleted(home):
    dest = home / ".grok" / "skills" / register_grok.GROK_SKILLS[0]
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("user's own variant\n", encoding="utf-8")
    rc = register_grok.main()
    assert rc == 0
    backups = [p for p in dest.parent.iterdir()
               if p.name.startswith(dest.name) and p != dest]
    assert backups, "differing user directory must be preserved as a backup"
    assert (dest / "SKILL.md").exists()              # now the engine-linked copy


# --- uninstall -------------------------------------------------------------------

def test_uninstall_removes_exact_links_only(home):
    register_grok.main()
    foreign = home / ".grok" / "skills" / "user-own-skill"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("mine\n", encoding="utf-8")
    result = register_grok.uninstall()
    assert result["profile"] == "removed"
    assert result["skills_removed"] == len(register_grok.GROK_SKILLS)
    assert foreign.exists(), "foreign skill must survive uninstall"
    for name in register_grok.GROK_SKILLS:
        assert not (home / ".grok" / "skills" / name).exists()
    for name in register_grok.GROK_SKILLS:
        assert (ENGINE / "singularity" / "skills" / name).is_dir(), \
            "uninstall must never touch the engine source"


def test_uninstall_keeps_foreign_dir_at_owned_name(home):
    """A REAL directory (not our link) sitting at an owned skill name is user
    content — counted as kept, never deleted."""
    dest = home / ".grok" / "skills" / register_grok.GROK_SKILLS[0]
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_text("user's own variant\n", encoding="utf-8")
    result = register_grok.uninstall()
    assert result["skills_kept"] >= 1
    assert dest.exists() and (dest / "SKILL.md").read_text(
        encoding="utf-8") == "user's own variant\n"


def test_install_uninstall_byte_round_trip(home):
    """User file before == user file after install->uninstall, byte for byte."""
    _profile(home).parent.mkdir(parents=True, exist_ok=True)
    original = "# my grok rules\nline one\n\nline two\n"
    _profile(home).write_text(original, encoding="utf-8")
    register_grok.main()
    register_grok.uninstall()
    assert _profile(home).read_text(encoding="utf-8") == original


def test_uninstall_when_absent_is_safe(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(register_grok.Path, "home",
                        classmethod(lambda cls: tmp_path))
    result = register_grok.uninstall()
    assert result == {"profile": "absent", "hooks": 0,
                      "skills_removed": 0, "skills_kept": 0}


# --- GRK-04: Stop capture hook registration --------------------------------------

def _hook_path(home):
    return home / ".grok" / "hooks" / "singularity.json"


def test_hook_registered_bom_less_with_safe_command(home):
    register_grok.main()
    raw = _hook_path(home).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "BOM'd hook JSON is silently skipped by grok"
    data = json.loads(raw.decode("utf-8"))
    cmd = data["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert "grok_stop_capture.py" in cmd
    assert "$env:" not in cmd            # load-time $-expansion would corrupt it
    if sys.platform.startswith("win"):
        assert cmd.startswith("& "), "PowerShell runner needs the call operator"
    else:
        assert not cmd.startswith("& ")


def test_hook_skipped_when_version_unproven(home, monkeypatch, capsys):
    monkeypatch.setattr(register_grok, "_grok_version", lambda: (0, 1, 0))
    register_grok.main()
    assert not _hook_path(home).exists()
    assert "stop-gate contract unproven" in capsys.readouterr().err


def test_hook_registration_idempotent(home):
    register_grok.main()
    first = _hook_path(home).read_bytes()
    register_grok.main()
    assert _hook_path(home).read_bytes() == first
    backups = [p for p in _hook_path(home).parent.iterdir()
               if p.name.startswith("singularity.json.bak")]
    assert backups == [], "an identical rerun must not create backups"


def test_foreign_file_at_hook_name_backed_up(home):
    p = _hook_path(home)
    p.parent.mkdir(parents=True)
    p.write_text('{"hooks": {"Stop": [{"hooks": [{"command": "user-own"}]}]}}\n',
                 encoding="utf-8")
    register_grok.main()
    backups = [b for b in p.parent.iterdir() if b.name.startswith("singularity.json.bak")]
    assert backups, "foreign content must be preserved as a timestamped backup"
    assert "user-own" in backups[0].read_text(encoding="utf-8")
    assert "grok_stop_capture.py" in p.read_text(encoding="utf-8")


def test_uninstall_removes_owned_hook_file(home):
    register_grok.main()
    result = register_grok.uninstall()
    assert result["hooks"] == 1
    assert not _hook_path(home).exists()


def test_uninstall_keeps_foreign_hook_file(home):
    p = _hook_path(home)
    p.parent.mkdir(parents=True)
    foreign = '{"hooks": {"Stop": [{"hooks": [{"command": "user-own"}]}]}}\n'
    p.write_text(foreign, encoding="utf-8")
    result = register_grok.uninstall()
    assert result["hooks"] == 0
    assert p.read_text(encoding="utf-8") == foreign
