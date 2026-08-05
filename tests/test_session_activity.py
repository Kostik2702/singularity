"""T09: normalized SessionActivity + platform-neutral capture-decision core.

Table-driven per the task's own AI prompt: read-only/no-action, one real
edit triggers, failed edits don't count, path containment (case/escaping),
marker/stop_hook_active/disabled short-circuits.
"""
import pytest

from singularity.telemetry.session_activity import (Decision, SessionActivity, decide,
                                          is_code_edit_path,
                                          is_sanctioned_capture_command,
                                          is_vault_write_path)


def _activity(**kw) -> SessionActivity:
    base = {"client": "claude", "session_id": "s1"}
    base.update(kw)
    return SessionActivity(**base)


# --- decide(): table-driven ------------------------------------------------------

DECIDE_CASES = [
    # (label, enabled, activity_kwargs, stop_hook_active, marker_exists, expected_action)
    ("read_only_no_activity", True, {}, False, False, "none"),
    ("one_real_edit_triggers", True, {"code_edits": 1}, False, False, "nudge"),
    ("multiple_edits_still_one_nudge_action", True, {"code_edits": 5}, False, False, "nudge"),
    ("zero_edits_never_triggers", True, {"code_edits": 0}, False, False, "none"),
    ("disabled_never_triggers", False, {"code_edits": 3}, False, False, "none"),
    ("stop_hook_active_never_stacks", True, {"code_edits": 3}, True, False, "none"),
    ("marker_present_silences", True, {"code_edits": 3}, False, True, "none"),
    ("vault_write_counts_as_captured", True,
     {"code_edits": 3, "vault_writes": 1}, False, False, "none"),
    ("sanctioned_capture_counts_as_captured", True,
     {"code_edits": 3, "sanctioned_captures": 1}, False, False, "none"),
    ("vault_write_alone_without_edits_is_still_none", True,
     {"code_edits": 0, "vault_writes": 1}, False, False, "none"),
    ("stop_hook_active_wins_over_everything", True,
     {"code_edits": 5, "vault_writes": 0}, True, True, "none"),
]


@pytest.mark.parametrize("label,enabled,kw,stop_active,marker,expected",
                         DECIDE_CASES, ids=[c[0] for c in DECIDE_CASES])
def test_decide_table(label, enabled, kw, stop_active, marker, expected):
    activity = _activity(**kw)
    result = decide(enabled, activity, stop_active, marker)
    assert isinstance(result, Decision)
    assert result.action == expected
    assert result.reason      # every decision carries a human-readable reason


def test_decide_never_raises_on_well_typed_input():
    for enabled in (True, False):
        for stop_active in (True, False):
            for marker in (True, False):
                decide(enabled, _activity(code_edits=1), stop_active, marker)


# --- is_code_edit_path -------------------------------------------------------------

def test_code_edit_inside_cwd_outside_vault(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    vault = tmp_path / "vault"; vault.mkdir()
    target = cwd / "src" / "app.py"
    assert is_code_edit_path(target, cwd, vault) is True


def test_code_edit_inside_vault_is_not_a_code_edit(tmp_path):
    cwd = tmp_path            # vault happens to be inside cwd
    vault = tmp_path / "vault"; vault.mkdir()
    target = vault / "learnings" / "active" / "T.md"
    assert is_code_edit_path(target, cwd, vault) is False


def test_code_edit_outside_cwd_is_false(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    vault = tmp_path / "vault"; vault.mkdir()
    outside = tmp_path / "elsewhere" / "x.py"
    assert is_code_edit_path(outside, cwd, vault) is False


def test_code_edit_no_vault_configured_still_requires_cwd_containment(tmp_path):
    cwd = tmp_path / "proj"; cwd.mkdir()
    inside = cwd / "x.py"
    outside = tmp_path / "elsewhere" / "x.py"
    assert is_code_edit_path(inside, cwd, None) is True
    assert is_code_edit_path(outside, cwd, None) is False


def test_code_edit_nonexistent_path_still_classified(tmp_path):
    # resolve() doesn't require the path to exist (strict=False default) —
    # a code edit that CREATES a new file must still classify correctly.
    cwd = tmp_path / "proj"; cwd.mkdir()
    vault = tmp_path / "vault"; vault.mkdir()
    new_file = cwd / "brand" / "new" / "file.py"
    assert is_code_edit_path(new_file, cwd, vault) is True


def test_code_edit_windows_case_insensitive_match(tmp_path, monkeypatch):
    if not __import__("sys").platform.startswith("win"):
        pytest.skip("Windows-only case-insensitivity check")
    cwd = tmp_path / "Proj"; cwd.mkdir()
    vault = tmp_path / "vault"; vault.mkdir()
    target = tmp_path / "PROJ" / "x.py"       # different case, same real dir
    assert is_code_edit_path(target, cwd, vault) is True


# --- is_vault_write_path -------------------------------------------------------------

def test_vault_write_inside_vault(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    target = vault / "learnings" / "active" / "T.md"
    assert is_vault_write_path(target, vault) is True


def test_vault_write_outside_vault_is_false(tmp_path):
    vault = tmp_path / "vault"; vault.mkdir()
    target = tmp_path / "proj" / "x.py"
    assert is_vault_write_path(target, vault) is False


# --- is_sanctioned_capture_command --------------------------------------------------

SANCTIONED_CASES = [
    ('mb learn "an insight" --task T1', True),
    ("mb note new howto Title", True),
    ('cd /d/proj && mb learn "x" --task T1', True),
    ("mb learn-report --whatever", False),          # word boundary: not "mb learn"
    ("npm install mb-learn-package", False),         # substring, not an actual mb invocation
    ("echo 'mb learn is great'", False),             # quoted text, not an invocation
    ("mb notebook convert", False),                  # "note" prefix but not "note new"
    ("git commit -m 'mb learn stuff'", False),
    ("mb learn", True),                              # bare form
]


@pytest.mark.parametrize("command,expected", SANCTIONED_CASES,
                         ids=[c[0][:30] for c in SANCTIONED_CASES])
def test_sanctioned_capture_command_table(command, expected):
    assert is_sanctioned_capture_command(command) is expected
