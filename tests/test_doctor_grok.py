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


def test_capture_skip_when_version_below_the_proven_contract(iso_home, monkeypatch):
    """An old but reachable CLI -> the stop-gate contract is unproven -> SKIP,
    and never a false PASS for a hook file that may not even be honored."""
    (iso_home / ".grok").mkdir()
    import subprocess as sp
    monkeypatch.setattr(shutil, "which", lambda _n: "grok")
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(
        a, 0, stdout="grok 0.1.0 (deadbeef)\n", stderr=""))
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F4-grok-capture") == "SKIP"


def test_capture_warns_when_the_binary_is_unreachable(iso_home):
    """Not a version SKIP (issue #10): ~/.grok exists but nothing can run the
    CLI, so the contract cannot be established at all — a different problem,
    and one the old message hid behind a version number."""
    (iso_home / ".grok").mkdir()
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F4-grok-capture") == "WARN"
    assert _status(rep, "F1-grok") == "WARN"
    detail = next(c["detail"] for c in rep.checks if c["id"] == "F1-grok")
    assert "unreachable" in detail


def _stub_proven_version(monkeypatch):
    """Make the freshly-loaded register_grok inside grok_block see a real
    binary at the proven contract version (shared module objects, so patching
    shutil/subprocess globally reaches it)."""
    import subprocess as sp
    monkeypatch.setattr(shutil, "which", lambda _n: "grok")
    monkeypatch.setattr(sp, "run", lambda *a, **k: sp.CompletedProcess(
        a, 0, stdout="grok 0.2.111 (94172f2aa4)\n", stderr=""))


def test_capture_pass_when_hook_registered(iso_home, monkeypatch):
    _stub_proven_version(monkeypatch)
    hook = iso_home / ".grok" / "hooks" / "singularity.json"
    hook.parent.mkdir(parents=True)
    hook.write_text('{"hooks": {"Stop": [{"hooks": [{"type": "command", '
                    '"command": "python x/grok_stop_capture.py"}]}]}}\n',
                    encoding="utf-8")
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F1-grok") == "PASS"
    assert _status(rep, "F4-grok-capture") == "PASS"


def test_capture_warn_when_hook_missing_at_proven_version(iso_home, monkeypatch):
    _stub_proven_version(monkeypatch)
    (iso_home / ".grok").mkdir()
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F4-grok-capture") == "WARN"


def test_capture_fail_when_hook_file_malformed(iso_home, monkeypatch):
    _stub_proven_version(monkeypatch)
    hook = iso_home / ".grok" / "hooks" / "singularity.json"
    hook.parent.mkdir(parents=True)
    hook.write_text("{ broken json", encoding="utf-8")
    rep = Report(quiet=True)
    grok_block(rep, fix=False)
    assert _status(rep, "F4-grok-capture") == "FAIL"


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
