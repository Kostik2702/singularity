"""Unit tests for install/register_hooks (Claude SessionStart/Stop/PreToolUse).

No dedicated coverage existed before T07 (register_hooks.py had zero tests —
confirmed in the T00 baseline inventory and still true through T01-T06).
Covers just what changed: the new PreToolUse (T07 module-context) entry
alongside the pre-existing SessionStart/Stop entries, ownership marking, and
idempotent re-registration — not a full audit of every pre-existing behavior.
"""
import importlib.util
import json
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location(
        "register_hooks", ENGINE / "install" / "register_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


register_hooks = _load()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(register_hooks.Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _settings(home):
    return home / ".claude" / "settings.json"


def test_fresh_install_creates_dot_claude_dir_when_absent(home):
    """Real bug found while writing this test suite (register_hooks.py had
    zero prior coverage): main() never mkdir'd ~/.claude before writing,
    so a machine where Claude Code has never run yet (no ~/.claude at all)
    would crash on first `mb install`. Fixed with parents=True, exist_ok=True."""
    assert not (home / ".claude").exists()
    rc = register_hooks.main()
    assert rc == 0
    assert (home / ".claude" / "settings.json").exists()


def test_fresh_install_registers_all_three_events(home):
    register_hooks.main()
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    hooks = data["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "PreToolUse"}


def test_pretooluse_entries_have_matcher_and_ownership_marker(home):
    register_hooks.main()
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    pre = data["hooks"]["PreToolUse"]
    assert len(pre) == 2                      # T07 module_context + T08 vault_guard
    assert all(e["_mb"] is True for e in pre)
    assert all(e["matcher"] == "Edit|Write|MultiEdit" for e in pre)
    commands = [e["hooks"][0]["command"] for e in pre]
    assert any("module_context.py" in c for c in commands)
    assert any("vault_guard.py" in c for c in commands)


def test_rerun_replaces_owned_entries_not_duplicates(home):
    register_hooks.main()
    register_hooks.main()
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert len(data["hooks"]["PreToolUse"]) == 2
    assert len(data["hooks"]["Stop"]) == 1
    assert len(data["hooks"]["SessionStart"]) == 1


def test_legacy_unmarked_entry_is_replaced_not_duplicated(home):
    """The actual bug found 2026-07-12 running this against a real machine:
    a pre-marker SessionStart entry (no `_mb`, but running our own script)
    was treated as foreign and a second, duplicate entry got ADDED instead
    of the legacy one being replaced. Fixed via _is_owned's command-name
    fallback — [[Rule - hook ownership detection must tolerate pre-marker
    installs]]."""
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text(json.dumps({"hooks": {"SessionStart": [
        {"hooks": [{"type": "command",
                    "command": '"...python.exe" ".../hooks/session_start.py"'}]}]}}),
        encoding="utf-8")
    register_hooks.main()
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    assert len(data["hooks"]["SessionStart"]) == 1
    assert data["hooks"]["SessionStart"][0]["_mb"] is True


def test_foreign_pretooluse_entries_preserved(home):
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "someone-elses-hook"}]}]}}),
        encoding="utf-8")
    register_hooks.main()
    data = json.loads(_settings(home).read_text(encoding="utf-8"))
    commands = [h["hooks"][0]["command"] for h in data["hooks"]["PreToolUse"]]
    assert "someone-elses-hook" in commands
    assert any("module_context.py" in c for c in commands)
    assert any("vault_guard.py" in c for c in commands)


def test_backup_created_when_settings_preexist(home):
    _settings(home).parent.mkdir(parents=True)
    _settings(home).write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    register_hooks.main()
    backups = list(_settings(home).parent.glob("settings.json.bak-*"))
    assert backups
