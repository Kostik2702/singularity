"""Unit tests for install/register_gemini (GCLI-02): managed GEMINI.md profile +
portable skill links, mirroring the Codex safety contract on the shared primitives.

Covers fresh/append/refresh/malformed markers, foreign-skill preservation, absent
client, old/unparseable version WARN (non-fatal), idempotency, the version parser,
and the missing-repo-skill failure code.
"""
import importlib.util
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
