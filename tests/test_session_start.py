"""Claude SessionStart hook (singularity/hooks/session_start.py).

No dedicated coverage existed before T02 (confirmed absent in the T00 baseline
inventory) — this file covers the pre-existing build_context() behavior plus
the T02 capture-hooks natural-language fragment it now injects.
"""
import io
import json
import time

import pytest

from singularity.hooks import session_start
from singularity.machine.hooks_state import CAPTURE_HOOKS_FRAGMENT


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


def _stdin_with_buffer(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    monkeypatch.setattr(session_start.sys.stdin, "buffer",
                        io.BytesIO(text.encode("utf-8")), raising=False)


# --- T06: project brain -----------------------------------------------------------

def _register_project(vault, project_id, project_path):
    mb = vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    (mb / "config.json").write_text(json.dumps(
        {"projects": {project_id: {"path": str(project_path)}}}), encoding="utf-8")


def _set_brain_note(vault, project_id, rel_note_path):
    mb = vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    (mb / "context.json").write_text(json.dumps(
        {project_id: {"brain_note": rel_note_path}}), encoding="utf-8")


def test_project_brain_injected_when_cwd_matches_registered_project(monkeypatch, vault):
    proj_dir = vault / "code" / "singularity"
    proj_dir.mkdir(parents=True)
    _register_project(vault, "singularity", proj_dir)
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Brain.md").write_text("---\ntype: reference\n---\n\nKey project fact.\n",
                                  encoding="utf-8")
    _set_brain_note(vault, "singularity", "reference/Brain.md")
    _resolve_to(monkeypatch, vault)

    ctx = session_start.build_context(event_cwd=str(proj_dir))
    assert "Key project fact." in ctx
    assert "Project brain (singularity)" in ctx


def test_unregistered_cwd_stays_general_no_brain_block(monkeypatch, vault):
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Brain.md").write_text("Key project fact.\n", encoding="utf-8")
    _set_brain_note(vault, "singularity", "reference/Brain.md")   # entry exists...
    _resolve_to(monkeypatch, vault)
    # ...but no project is REGISTERED, so cwd never resolves to "singularity"
    ctx = session_start.build_context(event_cwd=str(vault / "somewhere-else"))
    assert "Key project fact." not in ctx
    assert "Project brain" not in ctx


def test_missing_event_cwd_falls_back_to_process_cwd(monkeypatch, vault):
    proj_dir = vault / "code" / "here"
    proj_dir.mkdir(parents=True)
    _register_project(vault, "here", proj_dir)
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Brain.md").write_text("Process-cwd fact.\n", encoding="utf-8")
    _set_brain_note(vault, "here", "reference/Brain.md")
    _resolve_to(monkeypatch, vault)
    monkeypatch.setattr(session_start.os, "getcwd", lambda: str(proj_dir))

    ctx = session_start.build_context(event_cwd=None)     # no event cwd at all
    assert "Process-cwd fact." in ctx


def test_missing_brain_note_file_does_not_break_session(monkeypatch, vault):
    proj_dir = vault / "code" / "p"
    proj_dir.mkdir(parents=True)
    _register_project(vault, "p", proj_dir)
    _set_brain_note(vault, "p", "reference/DoesNotExist.md")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context(event_cwd=str(proj_dir))   # must not raise
    assert "SEM-FIRST" in ctx
    assert "Project brain" not in ctx


def test_malformed_context_json_does_not_break_session(monkeypatch, vault):
    proj_dir = vault / "code" / "p"
    proj_dir.mkdir(parents=True)
    _register_project(vault, "p", proj_dir)
    (vault / ".mb" / "context.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context(event_cwd=str(proj_dir))   # must not raise
    assert "SEM-FIRST" in ctx


def test_malformed_config_json_does_not_break_project_detection(monkeypatch, vault):
    mb = vault / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context(event_cwd=str(vault))      # must not raise
    assert "SEM-FIRST" in ctx


def test_path_traversal_brain_note_rejected(monkeypatch, vault, tmp_path):
    proj_dir = vault / "code" / "p"
    proj_dir.mkdir(parents=True)
    _register_project(vault, "p", proj_dir)
    outside = tmp_path.parent / "secret.md"
    outside.write_text("SECRET CONTENT", encoding="utf-8")
    _set_brain_note(vault, "p", "../../secret.md")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context(event_cwd=str(proj_dir))
    assert "SECRET CONTENT" not in ctx


def test_brain_max_lines_config_is_respected(monkeypatch, vault):
    proj_dir = vault / "code" / "p"
    proj_dir.mkdir(parents=True)
    mb = vault / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps(
        {"projects": {"p": {"path": str(proj_dir)}}, "brain_max_lines": 3}),
        encoding="utf-8")
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Brain.md").write_text("\n".join(f"fact {i}" for i in range(10)), encoding="utf-8")
    _set_brain_note(vault, "p", "reference/Brain.md")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context(event_cwd=str(proj_dir))
    assert "fact 2" in ctx and "fact 3" not in ctx


# --- T06: total session-context budget ---------------------------------------------

def test_session_context_max_lines_truncates_total_output(monkeypatch, vault):
    active = vault / "learnings" / "active"; active.mkdir(parents=True)
    for i in range(50):
        (active / f"TASK-{i}.md").write_text("x" * 200, encoding="utf-8")
    mb = vault / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps({"session_context_max_lines": 5}),
                                    encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert len(ctx.splitlines()) <= 6      # budget + one truncation-note line
    assert "session_context_max_lines=5" in ctx


# --- T06: main() extracts event cwd from stdin --------------------------------------

def test_main_extracts_cwd_from_event_and_uses_it(monkeypatch, vault, capsys):
    proj_dir = vault / "code" / "p"
    proj_dir.mkdir(parents=True)
    _register_project(vault, "p", proj_dir)
    ref = vault / "reference"; ref.mkdir(parents=True)
    (ref / "Brain.md").write_text("Event-cwd fact.\n", encoding="utf-8")
    _set_brain_note(vault, "p", "reference/Brain.md")
    _resolve_to(monkeypatch, vault)

    _stdin_with_buffer(monkeypatch, json.dumps({"cwd": str(proj_dir)}))
    rc = session_start.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "Event-cwd fact." in payload["hookSpecificOutput"]["additionalContext"]


def test_main_malformed_stdin_json_falls_back_gracefully(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    _stdin_with_buffer(monkeypatch, "not json at all {{{")
    rc = session_start.main()
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert "SEM-FIRST" in payload["hookSpecificOutput"]["additionalContext"]


# --- T12: digest nag ----------------------------------------------------------

def _write_digest_last(vault, entries, index_status="ok", age_hours=0):
    mb = vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    path = mb / "digest-last.json"
    path.write_text(json.dumps({"generated_at": "x", "entries": entries,
                                "index_status": index_status}), encoding="utf-8")
    if age_hours:
        import os
        old = time.time() - age_hours * 3600
        os.utime(path, (old, old))


def test_nag_shown_when_actionable_entries_above_threshold(monkeypatch, vault):
    _write_digest_last(vault, [
        {"file": "learnings/active/T.md", "section": "S1",
         "candidates": [{"file": "reference/R.md", "type": "reference", "score": 0.9}]},
    ])
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "learnings/active/T.md#S1" in ctx
    assert "reference/R.md" in ctx


def test_nag_writes_stamp_only_on_successful_show(monkeypatch, vault):
    _write_digest_last(vault, [
        {"file": "A.md", "section": "S",
         "candidates": [{"file": "B.md", "type": "reference", "score": 0.9}]},
    ])
    _resolve_to(monkeypatch, vault)
    session_start.build_context()
    assert (vault / ".mb" / "nag-state.json").exists()


def test_nag_no_stamp_when_no_actionable_entries(monkeypatch, vault):
    _write_digest_last(vault, [])
    _resolve_to(monkeypatch, vault)
    session_start.build_context()
    assert not (vault / ".mb" / "nag-state.json").exists()


def test_nag_rate_limited_within_72h(monkeypatch, vault):
    _write_digest_last(vault, [
        {"file": "A.md", "section": "S",
         "candidates": [{"file": "B.md", "type": "reference", "score": 0.9}]},
    ])
    mb = vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta
    recent = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    (mb / "nag-state.json").write_text(json.dumps({"last_shown_at": recent}), encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "A.md#S" not in ctx


def test_nag_shown_again_after_72h(monkeypatch, vault):
    _write_digest_last(vault, [
        {"file": "A.md", "section": "S",
         "candidates": [{"file": "B.md", "type": "reference", "score": 0.9}]},
    ])
    mb = vault / ".mb"
    mb.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timedelta
    old = (datetime.now() - timedelta(hours=73)).isoformat(timespec="seconds")
    (mb / "nag-state.json").write_text(json.dumps({"last_shown_at": old}), encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "A.md#S" in ctx


def test_nag_top_3_only(monkeypatch, vault):
    entries = [
        {"file": f"L{i}.md", "section": "S",
         "candidates": [{"file": "T.md", "type": "reference", "score": 0.9 - i * 0.01}]}
        for i in range(5)
    ]
    _write_digest_last(vault, entries)
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    shown = sum(1 for i in range(5) if f"L{i}.md" in ctx)
    assert shown == 3


def test_nag_below_threshold_not_shown(monkeypatch, vault):
    _write_digest_last(vault, [
        {"file": "A.md", "section": "S",
         "candidates": [{"file": "B.md", "type": "reference", "score": 0.1}]},
    ])
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "A.md#S" not in ctx


def test_nag_stale_cache_shows_bounded_message_not_full_nag(monkeypatch, vault):
    _write_digest_last(vault, [
        {"file": "A.md", "section": "S",
         "candidates": [{"file": "B.md", "type": "reference", "score": 0.9}]},
    ], age_hours=72)
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "stale" in ctx.lower()
    assert "A.md#S" not in ctx


def test_missing_digest_cache_is_silent(monkeypatch, vault):
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert "digest backlog" not in ctx
    # "nag" is three letters and matched "ma-nag-ed" the moment a fragment
    # mentioned a managed root. The needle is the nag's own wording.
    assert "curator suggests" not in ctx.lower()


def test_malformed_digest_cache_does_not_crash(monkeypatch, vault):
    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "digest-last.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()   # must not raise
    assert "SEM-FIRST" in ctx


def test_verification_fragment_reaches_every_session(monkeypatch, vault):
    """The main thread reads no preamble — this hint is its only carrier.

    Both failures behind the rule (an exit code taken from a pipe, an invariant
    that checked almost nothing) happened in the main thread, where the
    subagent preamble never arrives. If this fragment stops being injected the
    rule silently stops existing for the one place it was written for.
    """
    from singularity.machine.hooks_state import VERIFICATION_FRAGMENT

    vault.mkdir(exist_ok=True)
    _resolve_to(monkeypatch, vault)
    ctx = session_start.build_context()
    assert VERIFICATION_FRAGMENT in ctx
    # the two actions, not just the heading — a fragment trimmed to a slogan
    # would still pass a substring check on its title
    assert 'echo "EXIT=$?"' in ctx
    assert "PLANTING the violation" in ctx
    assert "`skipped` count" in ctx
