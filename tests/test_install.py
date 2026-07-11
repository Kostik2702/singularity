"""Installer-level tests (REV-6): register_hooks/register_skills/register_codex
return codes must be propagated, not silently swallowed.

install.py lives outside the singularity package (no __init__.py in install/),
loaded via importlib like the other install/*.py test files. register_hooks/
register_skills/register_codex are deferred, sibling (non-package) imports
inside main() — they're stubbed via sys.modules so main() picks them up
without needing install/ on sys.path.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

_spec = importlib.util.spec_from_file_location("install_main", ENGINE / "install" / "install.py")
install = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(install)

from singularity.config import Config  # noqa: E402


def _fake_module(name, rc):
    mod = types.ModuleType(name)
    mod.main = lambda: rc
    return mod


def _wire_happy_dependencies(monkeypatch, tmp_path, *, doctor_rc=0):
    """Stub every main()-internal import EXCEPT register_hooks/skills/codex
    (the caller sets those) so only the phase-4 failure-aggregation logic
    under test actually runs — Phase 1/3/3b/5 are skipped via CLI flags or
    replaced with trivial no-ops."""
    import singularity.config as config_mod
    import singularity.cli as cli_mod
    import singularity.search.indexer as indexer_mod
    import singularity.search.client as client_mod
    import singularity.doctor as doctor_mod

    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: tmp_path)
    monkeypatch.setattr(config_mod, "load_config",
                        lambda vault_arg=None: Config(vault=tmp_path, data={}))
    monkeypatch.setattr(config_mod, "write_pointer", lambda vault: None)
    monkeypatch.setattr(cli_mod, "cmd_init", lambda cfg, _args: 0)
    monkeypatch.setattr(indexer_mod, "rebuild", lambda cfg: {"notes": 0})
    monkeypatch.setattr(client_mod, "start_daemon", lambda cfg: True)
    monkeypatch.setattr(doctor_mod, "run",
                        lambda cfg, fix=False, heavy=False, as_json=False: doctor_rc)
    # register_agents is a required phase like hooks/skills; default it to
    # passing so tests that don't specifically exercise it don't ImportError.
    monkeypatch.setitem(sys.modules, "register_agents", _fake_module("register_agents", 0))


def test_install_succeeds_when_all_phases_pass(tmp_path, monkeypatch):
    _wire_happy_dependencies(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "register_hooks", _fake_module("register_hooks", 0))
    monkeypatch.setitem(sys.modules, "register_skills", _fake_module("register_skills", 0))
    monkeypatch.setitem(sys.modules, "register_codex", _fake_module("register_codex", 0))

    rc = install.main(["--skip-deps", "--skip-schedule", "--vault", str(tmp_path)])
    assert rc == 0


@pytest.mark.parametrize("failing",
                         ["register_hooks", "register_skills", "register_codex", "register_agents"])
def test_install_fails_when_a_required_phase_fails(tmp_path, monkeypatch, capsys, failing):
    _wire_happy_dependencies(monkeypatch, tmp_path)
    rcs = {"register_hooks": 0, "register_skills": 0, "register_codex": 0,
           "register_agents": 0, failing: 1}
    for name, rc in rcs.items():
        monkeypatch.setitem(sys.modules, name, _fake_module(name, rc))

    rc = install.main(["--skip-deps", "--skip-schedule", "--vault", str(tmp_path)])

    assert rc != 0
    err = capsys.readouterr().err
    assert "REQUIRED phase(s) failed" in err
    assert failing in err


def test_install_aggregates_multiple_required_failures_in_one_message(tmp_path, monkeypatch, capsys):
    _wire_happy_dependencies(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "register_hooks", _fake_module("register_hooks", 1))
    monkeypatch.setitem(sys.modules, "register_skills", _fake_module("register_skills", 1))
    monkeypatch.setitem(sys.modules, "register_codex", _fake_module("register_codex", 0))

    rc = install.main(["--skip-deps", "--skip-schedule", "--vault", str(tmp_path)])

    assert rc != 0
    err = capsys.readouterr().err
    assert "register_hooks" in err and "register_skills" in err
    assert "register_codex" not in err.split("REQUIRED phase(s) failed")[1]


def test_install_codex_not_present_is_not_a_failure(tmp_path, monkeypatch):
    """register_codex.main() legitimately returns 0 when Codex isn't installed
    on this machine (its own no-op skip) — that must never surface as a
    REQUIRED-phase failure."""
    _wire_happy_dependencies(monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "register_hooks", _fake_module("register_hooks", 0))
    monkeypatch.setitem(sys.modules, "register_skills", _fake_module("register_skills", 0))
    monkeypatch.setitem(sys.modules, "register_codex", _fake_module("register_codex", 0))

    rc = install.main(["--skip-deps", "--skip-schedule", "--vault", str(tmp_path)])
    assert rc == 0


def test_install_required_failure_overrides_a_passing_doctor(tmp_path, monkeypatch):
    """doctor can PASS on an incompletely-installed machine (it doesn't check
    every registration site) — a required-phase failure must win regardless
    of what doctor reports."""
    _wire_happy_dependencies(monkeypatch, tmp_path, doctor_rc=0)
    monkeypatch.setitem(sys.modules, "register_hooks", _fake_module("register_hooks", 1))
    monkeypatch.setitem(sys.modules, "register_skills", _fake_module("register_skills", 0))
    monkeypatch.setitem(sys.modules, "register_codex", _fake_module("register_codex", 0))

    rc = install.main(["--skip-deps", "--skip-schedule", "--vault", str(tmp_path)])
    assert rc == 1


def test_install_missing_vault_fails_fast_before_any_phase(tmp_path, monkeypatch):
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: tmp_path / "nope")
    # deliberately do NOT stub register_hooks/skills/codex — if the installer
    # reached Phase 4 it would ImportError (they're not on sys.path here)
    rc = install.main(["--skip-deps", "--skip-schedule"])
    assert rc == 2
