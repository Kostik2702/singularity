"""Claude SessionStart hook (singularity/hooks/session_start.py).

No dedicated coverage existed before T02 (confirmed absent in the T00 baseline
inventory) — this file covers the pre-existing build_context() behavior plus
the T02 capture-hooks natural-language fragment it now injects.
"""
import io
import json

import pytest

from singularity.hooks import session_start
from singularity.hooks_state import CAPTURE_HOOKS_FRAGMENT


@pytest.fixture
def vault(tmp_path):
    return tmp_path


def _resolve_to(monkeypatch, value):
    # build_context() does a late `from singularity.config import resolve_vault`
    # each call, so patching the source module is enough — matches test_install.py.
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: value)


def test_no_vault_configured_reports_clearly(monkeypatch):
    _resolve_to(monkeypatch, None)
    ctx = session_start.build_context()
    assert "No vault configured" in ctx


def test_vault_path_missing_on_disk_warns(monkeypatch, tmp_path):
    _resolve_to(monkeypatch, tmp_path / "does-not-exist")
    ctx = session_start.build_context()
    assert "WARNING" in ctx and "does not exist" in ctx


def test_fresh_vault_includes_sem_first_and_capture_hooks_fragment(monkeypatch, vault):
    vault.mkdir(exist_ok=True)
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "SEM-FIRST" in ctx
    assert CAPTURE_HOOKS_FRAGMENT in ctx
    assert "mb hooks status" in ctx and "mb hooks enable" in ctx and "mb hooks disable" in ctx


def test_active_tasks_listed(monkeypatch, vault):
    active = vault / "learnings" / "active"
    active.mkdir(parents=True)
    (active / "TASK-A.md").write_text("x", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "TASK-A" in ctx


def test_latest_changelog_heading_surfaced(monkeypatch, vault):
    changelog = vault / "changelog"
    changelog.mkdir(parents=True)
    (changelog / "2026-07.md").write_text(
        "# Changelog\n\n## 2026-07-12 — first entry\ntext\n", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "2026-07-12" in ctx and "first entry" in ctx


def test_doctor_warnings_surfaced_when_cached(monkeypatch, vault):
    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "doctor-last.json").write_text(json.dumps(
        {"checks": [{"id": "C2-watcher", "status": "WARN", "detail": "x"}]}),
        encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "C2-watcher=WARN" in ctx


def test_broken_doctor_cache_does_not_crash(monkeypatch, vault):
    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "doctor-last.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()          # must not raise
    assert "SEM-FIRST" in ctx


def test_main_never_raises_and_emits_valid_json(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    rc = session_start.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "SEM-FIRST" in payload["hookSpecificOutput"]["additionalContext"]
