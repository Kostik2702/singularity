"""GRK-04: Grok Stop capture adapter (singularity/hooks/grok_stop_capture.py).

Covers the chat_history.jsonl parser (success/failure classification pinned by
the GRK-00/GRK-04 fixtures), the decision wiring (auto_capture, once-per-session
marker with client=grok, stopHookActive, reason gating), the camelCase-first /
snake_case-fallback envelope, and fail-open behavior.
"""
import io
import json
from pathlib import Path

import pytest

from singularity.hooks import grok_stop_capture as hook

VAULT_NAME = "vault"


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / VAULT_NAME
    (v / ".mb").mkdir(parents=True)
    return v


@pytest.fixture
def session(tmp_path):
    """A fake grok session dir with an updates.jsonl transcript path and a
    sibling chat_history.jsonl the adapter actually parses."""
    sdir = tmp_path / "sessions" / "cwd-enc" / "sid-1"
    sdir.mkdir(parents=True)
    (sdir / "updates.jsonl").write_text("", encoding="utf-8")
    return sdir


def _history(sdir, rows):
    (sdir / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _edit_rows(file_path, result="The file X has been created."):
    return [
        {"type": "assistant", "content": "", "model_id": "m", "tool_calls": [
            {"id": "c1", "name": "write",
             "arguments": json.dumps({"file_path": file_path, "content": "x"})}]},
        {"type": "tool_result", "tool_call_id": "c1", "content": result},
    ]


def _run_main(monkeypatch, capsys, vault, event):
    import singularity.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "resolve_vault", lambda *_a, **_k: vault)
    monkeypatch.setattr(hook, "resolve_vault", lambda *_a, **_k: vault, raising=False)
    monkeypatch.setattr(hook.sys, "stdin", type("S", (), {
        "buffer": io.BytesIO(json.dumps(event).encode("utf-8"))})())
    rc = hook.main()
    out = capsys.readouterr().out.strip()
    return rc, (json.loads(out) if out else None)


def _event(sdir, cwd, **over):
    event = {
        "hookEventName": "stop",
        "sessionId": "sid-1",
        "cwd": str(cwd),
        "workspaceRoot": str(cwd),
        "transcriptPath": str(sdir / "updates.jsonl"),
        "reason": "end_turn",
        "stopHookActive": False,
    }
    event.update(over)
    return event


# --- parser ---------------------------------------------------------------------

def test_parser_counts_successful_code_edit(tmp_path, vault, session):
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py")))
    act = hook.parse_chat_history(session / "chat_history.jsonl", "sid-1",
                                  str(proj), vault)
    assert act.code_edits == 1 and act.vault_writes == 0


def test_parser_classifies_vault_write(tmp_path, vault, session):
    _history(session, _edit_rows(str(vault / "note.md")))
    act = hook.parse_chat_history(session / "chat_history.jsonl", "sid-1",
                                  str(tmp_path), vault)
    assert act.vault_writes == 1 and act.code_edits == 0


@pytest.mark.parametrize("failure", [
    "Tool `write` failed: IO Error: failed to create whole tree",
    "Failed to parse arguments for tool `write`: missing field `file_path`",
])
def test_parser_skips_failed_edits(tmp_path, vault, session, failure):
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py"), result=failure))
    act = hook.parse_chat_history(session / "chat_history.jsonl", "sid-1",
                                  str(proj), vault)
    assert act.code_edits == 0


def test_parser_counts_sanctioned_capture_only_on_exit0(tmp_path, vault, session):
    rows = [
        {"type": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "run_terminal_command",
             "arguments": json.dumps({"command": "mb learn \"insight\" --task T", "description": "d"})}]},
        {"type": "tool_result", "tool_call_id": "c1", "content": "exit: 0\nok\n"},
        {"type": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "name": "run_terminal_command",
             "arguments": json.dumps({"command": "mb learn \"again\"", "description": "d"})}]},
        {"type": "tool_result", "tool_call_id": "c2", "content": "exit: 1\nboom\n"},
    ]
    _history(session, rows)
    act = hook.parse_chat_history(session / "chat_history.jsonl", "sid-1",
                                  str(tmp_path), vault)
    assert act.sanctioned_captures == 1


def test_parser_unreadable_history_fails_open(tmp_path, vault, session):
    act = hook.parse_chat_history(session / "chat_history.jsonl", "sid-1",
                                  str(tmp_path), vault)          # file absent
    assert act.code_edits == 0 and act.parse_warnings


# --- main() wiring ----------------------------------------------------------------

def test_nudges_once_on_uncaptured_code_edit(tmp_path, vault, session, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py")))
    rc, out = _run_main(monkeypatch, capsys, vault, _event(session, proj))
    assert rc == 0
    assert out and out["decision"] == "block" and out["reason"]
    marker = vault / ".mb" / "markers" / "capture-grok-sid-1"
    assert marker.exists()
    # second fire in the same session: marker suppresses a repeat nudge
    rc2, out2 = _run_main(monkeypatch, capsys, vault, _event(session, proj))
    assert rc2 == 0 and out2 is None


def test_snake_case_fallback_envelope(tmp_path, vault, session, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py")))
    event = {"session_id": "sid-1", "cwd": str(proj), "reason": "end_turn",
             "stop_hook_active": False,
             "transcript_path": str(session / "updates.jsonl")}
    rc, out = _run_main(monkeypatch, capsys, vault, event)
    assert rc == 0 and out and out["decision"] == "block"


@pytest.mark.parametrize("mutation,expect_out", [
    ({"reason": "channel_closed"}, None),          # session-end observe fire
    ({"stopHookActive": True}, None),              # never stack nudges
])
def test_gating_never_nudges(tmp_path, vault, session, monkeypatch, capsys,
                             mutation, expect_out):
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py")))
    rc, out = _run_main(monkeypatch, capsys, vault,
                        _event(session, proj, **mutation))
    assert rc == 0 and out is expect_out


def test_disabled_auto_capture_never_nudges(tmp_path, vault, session, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text(
        json.dumps({"auto_capture": False}), encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py")))
    rc, out = _run_main(monkeypatch, capsys, vault, _event(session, proj))
    assert rc == 0 and out is None


def test_session_already_captured_never_nudges(tmp_path, vault, session, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    rows = (_edit_rows(str(proj / "a.py"))
            + [{"type": "assistant", "content": "", "tool_calls": [
                   {"id": "c9", "name": "run_terminal_command",
                    "arguments": json.dumps({"command": "mb learn \"x\"", "description": "d"})}]},
               {"type": "tool_result", "tool_call_id": "c9", "content": "exit: 0\n"}])
    _history(session, rows)
    rc, out = _run_main(monkeypatch, capsys, vault, _event(session, proj))
    assert rc == 0 and out is None


def test_trivial_session_never_nudges(tmp_path, vault, session, monkeypatch, capsys):
    _history(session, [{"type": "assistant", "content": "hi", "model_id": "m"}])
    rc, out = _run_main(monkeypatch, capsys, vault,
                        _event(session, tmp_path / "proj"))
    assert rc == 0 and out is None


def test_malformed_stdin_fails_open(monkeypatch, capsys, vault):
    monkeypatch.setattr(hook.sys, "stdin", type("S", (), {
        "buffer": io.BytesIO(b"{not json")})())
    assert hook.main() == 0
    assert capsys.readouterr().out.strip() == ""


def test_bom_stdin_tolerated(tmp_path, vault, session, monkeypatch, capsys):
    proj = tmp_path / "proj"
    proj.mkdir()
    _history(session, _edit_rows(str(proj / "a.py")))
    import singularity.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "resolve_vault", lambda *_a, **_k: vault)
    payload = b"\xef\xbb\xbf" + json.dumps(_event(session, proj)).encode("utf-8")
    monkeypatch.setattr(hook.sys, "stdin", type("S", (), {"buffer": io.BytesIO(payload)})())
    assert hook.main() == 0
    out = capsys.readouterr().out.strip()
    assert json.loads(out)["decision"] == "block"
