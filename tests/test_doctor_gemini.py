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
    assert _status(rep, "E1-gemini") == "WARN"          # version unreadable (no binary)
    assert _status(rep, "E2-gemini-profile") == "WARN"
    assert _status(rep, "E3-gemini-skills") == "WARN"
    assert _status(rep, "E4-gemini-hooks") == "SKIP"    # hooks need proven version


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
