"""T01: capture-hooks desired/installed/effective state service + `mb hooks`.

Scope is capture hooks only (Claude Stop, Gemini AfterAgent) — SessionStart is
never touched by anything under test here.
"""
import json

import pytest

from singularity import hooks_state
from singularity.config import Config


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    (v / ".mb").mkdir(parents=True)
    return v


def _write_config(vault, data):
    (vault / ".mb" / "config.json").write_text(json.dumps(data), encoding="utf-8")


# --- capture_config_state -----------------------------------------------------

def test_default_state_when_no_config_file(vault):
    state = hooks_state.capture_config_state(vault)
    assert state == {"configured": True, "default": True, "source": "default"}


def test_default_state_when_key_absent(vault):
    _write_config(vault, {"model": "m"})
    state = hooks_state.capture_config_state(vault)
    assert state["configured"] is True
    assert state["source"] == "default"


def test_explicit_true(vault):
    _write_config(vault, {"auto_capture": True})
    assert hooks_state.capture_config_state(vault) == {
        "configured": True, "default": True, "source": "explicit"}


def test_explicit_false(vault):
    _write_config(vault, {"auto_capture": False})
    assert hooks_state.capture_config_state(vault) == {
        "configured": False, "default": True, "source": "explicit"}


def test_malformed_json_is_source_error_not_a_crash(vault):
    (vault / ".mb" / "config.json").write_text("{not json", encoding="utf-8")
    state = hooks_state.capture_config_state(vault)
    assert state["source"] == "error"
    assert state["configured"] is True     # fail-open default
    assert "error" in state


def test_non_bool_auto_capture_is_source_error(vault):
    _write_config(vault, {"auto_capture": "yes"})
    state = hooks_state.capture_config_state(vault)
    assert state["source"] == "error"
    assert state["configured"] is True


# --- per-client installed probes ----------------------------------------------

def test_claude_not_installed_when_settings_absent(home):
    assert hooks_state._claude_installed() is False


def test_claude_installed_when_mb_marked_stop_entry_present(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"_mb": True, "hooks": [{"type": "command", "command": "x"}]}]}
    }), encoding="utf-8")
    assert hooks_state._claude_installed() is True


def test_claude_installed_via_legacy_unmarked_entry_matching_owned_script(home):
    """Real machine found (2026-07-12) with a Stop entry that predates the
    `_mb` marker: no marker, but the command still points at stop_capture.py."""
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command",
                                       "command": '"...python.exe" ".../singularity/hooks/stop_capture.py"'}]}]}
    }), encoding="utf-8")
    assert hooks_state._claude_installed() is True


def test_claude_not_installed_when_only_foreign_stop_entry(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "someone-elses"}]}]}
    }), encoding="utf-8")
    assert hooks_state._claude_installed() is False


def test_gemini_installed_when_named_after_agent_hook_present(home):
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"AfterAgent": [{"hooks": [
            {"type": "command", "command": "x", "name": "singularity-after-agent"}]}]}
    }), encoding="utf-8")
    assert hooks_state._gemini_installed() is True


def test_gemini_not_installed_when_absent(home):
    assert hooks_state._gemini_installed() is False


def test_gemini_not_installed_when_only_foreign_named_hook(home):
    settings = home / ".gemini" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"AfterAgent": [{"hooks": [
            {"type": "command", "command": "x", "name": "someone-elses"}]}]}
    }), encoding="utf-8")
    assert hooks_state._gemini_installed() is False


# --- client_states / status -----------------------------------------------------

def test_codex_always_reported_unsupported(home, vault):
    (home / ".codex").mkdir()          # even when Codex IS present on the machine
    states = hooks_state.client_states(vault)
    assert states["codex"] == {"installed": False, "supported": False,
                               "mode": "none", "effective": False}


def test_effective_requires_installed_and_enabled(home, vault):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({
        "hooks": {"Stop": [{"_mb": True, "hooks": [{"type": "command", "command": "x"}]}]}
    }), encoding="utf-8")
    _write_config(vault, {"auto_capture": False})

    states = hooks_state.client_states(vault)
    assert states["claude"]["installed"] is True
    assert states["claude"]["effective"] is False       # installed but disabled


def test_status_shape_default(home, vault):
    data = hooks_state.status(vault)
    assert data["kind"] == "capture_hooks"
    assert data["configured"] is True
    assert data["default"] is True
    assert data["source"] == "default"
    assert set(data["clients"]) == {"claude", "gemini", "codex"}
    assert data["warnings"] == []
    json.dumps(data)   # JSON-serializable


def test_status_carries_warning_on_malformed_config(home, vault):
    (vault / ".mb" / "config.json").write_text("{not json", encoding="utf-8")
    data = hooks_state.status(vault)
    assert data["source"] == "error"
    assert data["warnings"]


# --- set_capture_enabled --------------------------------------------------------

def test_set_capture_enabled_writes_atomic_and_preserves_other_keys(vault):
    cfg = Config(vault=vault, data={"model": "m", "min_score": 0.5})
    hooks_state.set_capture_enabled(cfg, False)
    on_disk = json.loads((vault / ".mb" / "config.json").read_text(encoding="utf-8"))
    assert on_disk["auto_capture"] is False
    assert on_disk["model"] == "m"
    leftovers = [p for p in (vault / ".mb").iterdir()
                if p.name.startswith("config") and p.suffix != ".json"]
    assert leftovers == []


def test_set_capture_enabled_idempotent(vault):
    cfg = Config(vault=vault, data={})
    hooks_state.set_capture_enabled(cfg, True)
    hooks_state.set_capture_enabled(cfg, True)
    assert cfg.data["auto_capture"] is True
    assert hooks_state.capture_config_state(vault)["configured"] is True


def test_set_capture_enabled_round_trip(vault):
    cfg = Config(vault=vault, data={})
    hooks_state.set_capture_enabled(cfg, False)
    assert hooks_state.capture_config_state(vault)["configured"] is False
    hooks_state.set_capture_enabled(cfg, True)
    assert hooks_state.capture_config_state(vault)["configured"] is True


def test_set_capture_enabled_rejects_invalid_candidate_without_writing(vault):
    cfg = Config(vault=vault, data={"min_score": 5.0})   # out of [0,1] range
    with pytest.raises(ValueError, match="min_score"):
        hooks_state.set_capture_enabled(cfg, True)
    assert not (vault / ".mb" / "config.json").exists()
