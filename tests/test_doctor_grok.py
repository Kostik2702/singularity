"""GRK-03: doctor Block F (Grok) — verifies a working path, not mere presence,
and --fix runs only the safe registrar. Tested via the module-level grok_block
against a fully isolated home (no real GROK_HOME/~/.grok, no binary on PATH).

Default doctor path stays local, read-only, auth-free and network-free: the
only external touch is `grok --version` (stubbed here); no session is ever
started (GRK-00: sessions auto-connect real MCP servers)."""
import importlib.util
import pathlib
import shutil

import pytest

from singularity.doctor import Report, grok_block

ENGINE = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "register_grok", ENGINE / "install" / "register_grok.py")
register_grok = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_grok)


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda _n: None)   # no binary -> version None
    return tmp_path


def _status(rep, check_id):
    for c in rep.checks:
        if c["id"] == check_id:
            return c["status"]
    return None


def test_absent_client_single_skip(iso_home):
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F1-grok") == "SKIP"
    assert _status(rep, "F2-grok-profile") is None      # block returned early


def test_present_but_bare_warns_no_false_pass(iso_home):
    """A present client with missing profile/skills must be WARN, never PASS."""
    (iso_home / ".grok").mkdir()
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F1-grok") == "WARN"            # version unreadable (no binary)
    assert _status(rep, "F2-grok-profile") == "WARN"
    assert _status(rep, "F3-grok-skills") == "WARN"


def test_deployed_profile_and_skills_pass(iso_home):
    g = iso_home / ".grok"
    (g / "skills").mkdir(parents=True)
    (g / "AGENTS.md").write_text(register_grok.BEGIN + "\nbody\n"
                                 + register_grok.END + "\n", encoding="utf-8")
    for name in register_grok.GROK_SKILLS:
        (g / "skills" / name).mkdir()
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F2-grok-profile") == "PASS"
    assert _status(rep, "F3-grok-skills") == "PASS"


def test_grok_home_env_override_is_checked(tmp_path, monkeypatch):
    """The doctor must inspect $GROK_HOME, not ~/.grok, when the env var is set —
    same resolver as the registrar (GRK-02)."""
    custom = tmp_path / "custom-root"
    (custom / "skills").mkdir(parents=True)
    (custom / "AGENTS.md").write_text(register_grok.BEGIN + "\nbody\n"
                                      + register_grok.END + "\n", encoding="utf-8")
    for name in register_grok.GROK_SKILLS:
        (custom / "skills" / name).mkdir()
    monkeypatch.setenv("GROK_HOME", str(custom))
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F2-grok-profile") == "PASS"
    assert _status(rep, "F3-grok-skills") == "PASS"


def test_capture_capability_is_skip_never_pass(iso_home):
    """GRK-04 has not shipped a runtime hook — a PASS here would be a false
    capability claim (hooks_state also reports supported:false)."""
    (iso_home / ".grok").mkdir()
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F4-grok-capture") == "SKIP"


def test_fix_runs_safe_registrar_and_marks_fixed(iso_home, monkeypatch):
    (iso_home / ".grok").mkdir()
    # grok_block loads a FRESH register_grok; patch the source so its
    # `from singularity.config import resolve_vault` binds the stub.
    import singularity.config as cfg
    monkeypatch.setattr(cfg, "resolve_vault", lambda *a, **k: pathlib.Path("D:/vault"))
    rep = Report(quiet=True)
    grok_block(rep, fix=True)                           # should deploy profile + skills
    assert _status(rep, "F2-grok-profile") == "FIXED"
    assert _status(rep, "F3-grok-skills") == "FIXED"
    assert (iso_home / ".grok" / "AGENTS.md").exists()


def test_fix_does_not_rerun_when_healthy(iso_home, monkeypatch):
    g = iso_home / ".grok"
    (g / "skills").mkdir(parents=True)
    (g / "AGENTS.md").write_text(register_grok.BEGIN + "\nbody\n"
                                 + register_grok.END + "\n", encoding="utf-8")
    for name in register_grok.GROK_SKILLS:
        (g / "skills" / name).mkdir()
    rep = Report(quiet=True)
    grok_block(rep, fix=True)
    assert _status(rep, "F2-grok-profile") == "PASS"    # not FIXED: nothing to fix
    assert _status(rep, "F3-grok-skills") == "PASS"
