"""Golden/regression tests for install/register_codex — pin the CURRENT observable
behaviour BEFORE the GCLI-01 refactor onto shared client_registration primitives.

These must stay green across the refactor: the resulting ~/.codex/AGENTS.md bytes,
the created/refreshed/appended status words, the council-skill exclusion, the
absent-client skip and the skill-conflict backup are the regression oracle for the
shared-primitives extraction.
"""
import importlib.util
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "register_codex", ENGINE / "install" / "register_codex.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


register_codex = _load()

VAULT = Path("D:/obsidian/memory brain")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Isolated HOME; deterministic 'codex present' with no real binary on PATH;
    fixed vault so profile bytes are stable."""
    monkeypatch.setattr(register_codex.Path, "home",
                        classmethod(lambda cls: tmp_path))
    # also patch register_skills.Path.home (it resolves ~/.claude independently)
    monkeypatch.setattr(register_codex.register_skills.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_codex.shutil, "which", lambda _n: None)
    monkeypatch.setattr(register_codex, "resolve_vault", lambda: VAULT)
    (tmp_path / ".codex").mkdir()          # simulate installed Codex
    return tmp_path


def _agents(home):
    return home / ".codex" / "AGENTS.md"


# --- absent client -------------------------------------------------------------

def test_absent_client_skips_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(register_codex.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(register_codex.shutil, "which", lambda _n: None)
    rc = register_codex.main()
    assert rc == 0
    assert not (tmp_path / ".codex").exists()   # nothing created on skip


# --- fresh ---------------------------------------------------------------------

def test_fresh_creates_profile_with_block(home):
    rc = register_codex.main()
    assert rc == 0
    text = _agents(home).read_text(encoding="utf-8")
    assert text.startswith(register_codex.BEGIN)
    assert text.rstrip().endswith(register_codex.END)
    assert str(VAULT) in text
    assert "SEM-FIRST" in text
    assert text.endswith("\n")


# --- T02: shared capture-hooks natural-language fragment ----------------------

def test_profile_contains_capture_hooks_fragment(home):
    register_codex.main()
    text = _agents(home).read_text(encoding="utf-8")
    assert "mb hooks status" in text and "mb hooks enable" in text and "mb hooks disable" in text
    assert "stop hooks" in text                 # EN alias present
    assert "auto-capture" in text


def test_capture_hooks_fragment_is_the_shared_constant(home):
    register_codex.main()
    text = _agents(home).read_text(encoding="utf-8")
    assert register_codex.CAPTURE_HOOKS_FRAGMENT in text        # verbatim, single source


def test_council_skills_never_shipped_to_codex(home):
    register_codex.main()
    text = _agents(home).read_text(encoding="utf-8")
    for council in ("council", "planning-council", "observation-council"):
        assert council not in register_codex.CODEX_SKILLS
    # the mirrored-skills line lists only CLI skills
    assert "mb-search" in text


# --- append (user file, no markers) -------------------------------------------

def test_append_preserves_user_content(home):
    user_text = "# My own AGENTS notes\n\nDo not touch this.\n"
    _agents(home).write_text(user_text, encoding="utf-8")
    register_codex.main()
    text = _agents(home).read_text(encoding="utf-8")
    assert "Do not touch this." in text          # user content intact
    assert register_codex.BEGIN in text          # our block appended after it
    assert text.index("Do not touch this.") < text.index(register_codex.BEGIN)


# --- refresh (existing block between user content) ----------------------------

def test_refresh_replaces_only_block(home):
    pre = "# Top user note\n"
    post = "\n# Bottom user note\n"
    _agents(home).write_text(
        pre + "\n" + register_codex.BEGIN + "\nOLD STALE BODY\n" + register_codex.END + post,
        encoding="utf-8")
    register_codex.main()
    text = _agents(home).read_text(encoding="utf-8")
    assert "OLD STALE BODY" not in text          # stale body replaced
    assert "Top user note" in text and "Bottom user note" in text
    assert text.count(register_codex.BEGIN) == 1  # exactly one managed block
    assert text.count(register_codex.END) == 1


# --- malformed markers (BEGIN without END) ------------------------------------

def test_malformed_markers_fall_back_to_append(home):
    """Only BEGIN present (no END): current code cannot safely splice, so it
    appends a fresh block and leaves the malformed text untouched."""
    broken = "user stuff\n" + register_codex.BEGIN + "\nhalf a block, no end\n"
    _agents(home).write_text(broken, encoding="utf-8")
    register_codex.main()
    text = _agents(home).read_text(encoding="utf-8")
    assert "half a block, no end" in text        # malformed original preserved
    assert text.rstrip().endswith(register_codex.END)  # a complete block appended


# --- idempotency ---------------------------------------------------------------

def test_idempotent_rerun_is_byte_stable(home):
    register_codex.main()
    first = _agents(home).read_bytes()
    register_codex.main()
    assert _agents(home).read_bytes() == first   # second run changes nothing


# --- skill conflict backup -----------------------------------------------------

def test_conflicting_skill_dir_backed_up_not_lost(home):
    dest = home / ".codex" / "skills"
    dest.mkdir(parents=True)
    name = register_codex.CODEX_SKILLS[0]
    conflicting = dest / name
    conflicting.mkdir()
    (conflicting / "user.txt").write_text("precious local edit", encoding="utf-8")
    register_codex.main()
    backups = list(dest.glob(f"{name}.backup-*"))
    assert backups, "differing skill dir must be preserved as a backup"
    assert (backups[0] / "user.txt").read_text(encoding="utf-8") == "precious local edit"
