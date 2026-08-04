"""GCLI-05: doctor Block E (Gemini) — verifies a working path, not mere presence,
and --fix runs only the safe registrar. Tested via the module-level gemini_block
against a fully isolated HOME (no real ~/.gemini, no binary on PATH)."""
import importlib.util
import pathlib
import shutil

import pytest

from singularity.doctor import Report, gemini_block

ENGINE = pathlib.Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "register_gemini", ENGINE / "install" / "register_gemini.py")
register_gemini = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(register_gemini)


@pytest.fixture
def iso_home(tmp_path, monkeypatch):
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
    gemini_block(rep, fix=False)
    assert _status(rep, "E1-gemini") == "SKIP"
    assert _status(rep, "E2-gemini-profile") is None   # block returned early


def test_present_but_bare_warns(iso_home):
    (iso_home / ".gemini").mkdir()
    rep = Report(quiet=True)
    gemini_block(rep, fix=False)
    assert _status(rep, "E1-gemini") == "WARN"          # binary unreachable
    assert _status(rep, "E2-gemini-profile") == "WARN"
    assert _status(rep, "E3-gemini-skills") == "WARN"
    # NOT a version SKIP any more (issue #10): the binary being unreachable is
    # a different problem from the version being too old, and saying "requires
    # gemini >= 0.26.0" to someone running 0.53.1 sent them looking in the
    # wrong place.
    assert _status(rep, "E4-gemini-hooks") == "WARN"


def _detail(rep, check_id):
    for c in rep.checks:
        if c["id"] == check_id:
            return c["detail"] + " || " + c["fix"]
    return ""


def test_unreachable_binary_is_reported_as_such_not_as_a_version_problem(iso_home):
    """The reported machine: ~/.gemini exists, gemini 0.53.1 installed under
    nvm, invisible to a non-interactive installer."""
    (iso_home / ".gemini").mkdir()
    rep = Report(quiet=True)
    gemini_block(rep, fix=False)

    e1 = _detail(rep, "E1-gemini")
    assert "unreachable" in e1
    assert "interactive shell" in e1
    assert "0.26" not in e1                      # no misleading version constraint
    assert "0.26" not in _detail(rep, "E4-gemini-hooks")


def test_fix_can_repair_hooks_when_the_version_is_unreadable(iso_home, monkeypatch):
    """Point 3 of the report: the old gate was `supported`, which is False
    whenever the version is unknown — so `--fix` could not reach the hook
    branch in exactly the state that needed it."""
    g = iso_home / ".gemini"
    (g / "skills").mkdir(parents=True)
    (g / "GEMINI.md").write_text(register_gemini.BEGIN + "\nbody\n"
                                 + register_gemini.END + "\n", encoding="utf-8")
    for name in register_gemini.GEMINI_SKILLS:
        (g / "skills" / name).mkdir()

    import singularity.doctor as doctor_mod
    real_load = doctor_mod._load_install
    ran = []

    def fake_load(name):
        mod = real_load(name)
        if name == "register_gemini":
            # located, but `--version` unreadable
            mod._gemini_exe = lambda: ("/nvm/bin/gemini", "/nvm/bin")
            mod._gemini_version = lambda: None
            mod.main = lambda: ran.append("registrar") or 0
        return mod

    monkeypatch.setattr(doctor_mod, "_load_install", fake_load)
    rep = Report(quiet=True)
    gemini_block(rep, fix=True)

    assert ran == ["registrar"]                  # the repair actually ran


def test_deployed_profile_and_skills_pass(iso_home):
    g = iso_home / ".gemini"
    (g / "skills").mkdir(parents=True)
    (g / "GEMINI.md").write_text(register_gemini.BEGIN + "\nbody\n"
                                 + register_gemini.END + "\n", encoding="utf-8")
    for name in register_gemini.GEMINI_SKILLS:
        (g / "skills" / name).mkdir()
    rep = Report(quiet=True)
    gemini_block(rep, fix=False)
    assert _status(rep, "E2-gemini-profile") == "PASS"
    assert _status(rep, "E3-gemini-skills") == "PASS"


def test_malformed_settings_is_fail(iso_home):
    g = iso_home / ".gemini"
    g.mkdir()
    (g / "settings.json").write_text("{ broken json", encoding="utf-8")
    rep = Report(quiet=True)
    gemini_block(rep, fix=False)
    assert _status(rep, "E4-gemini-hooks") == "FAIL"


def test_fix_runs_safe_registrar_and_marks_fixed(iso_home, monkeypatch):
    (iso_home / ".gemini").mkdir()
    # gemini_block loads a FRESH register_gemini; patch the source so its
    # `from singularity.config import resolve_vault` binds the stub.
    import singularity.config as cfg
    monkeypatch.setattr(cfg, "resolve_vault", lambda *a, **k: pathlib.Path("D:/vault"))
    rep = Report(quiet=True)
    gemini_block(rep, fix=True)                          # should deploy profile + skills
    assert _status(rep, "E2-gemini-profile") == "FIXED"
    assert _status(rep, "E3-gemini-skills") == "FIXED"
    assert (iso_home / ".gemini" / "GEMINI.md").exists()
