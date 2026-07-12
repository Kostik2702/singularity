"""Unit tests for the Gemini AfterAgent auto-capture nudge (T10).

Key safety property: the hook NEVER emits `decision` (deny) — only a passive
`systemMessage` — because GCLI-00 proved deny mutates headless `-o json`. All
guard cases emit an empty object and the nudge fires at most once per session.

Decision is now code-edit based (T09's SessionActivity/decide()), not a raw
tool-count threshold — fixtures build realistic `toolCalls[]` entries
(status/args), matching tests/fixtures/gemini/transcript_tooluse.jsonl's
real, redacted shape, rather than a generic tool count.
"""
import io
import json

import pytest

from singularity.hooks import gemini_after_agent as ga


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / ".mb").mkdir(parents=True)
    import singularity.config as cfg
    monkeypatch.setattr(cfg, "resolve_vault", lambda *_a, **_k: v)
    return v


@pytest.fixture
def proj(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    return p


def _write_call(abs_path, call_id="w1", status="success"):
    return {"id": call_id, "name": "write_file",
            "args": {"file_path": str(abs_path), "content": "x"}, "status": status}


def _shell_call(command, call_id="s1"):
    return {"id": call_id, "name": "run_shell_command",
            "args": {"command": command}, "status": "success"}


def _read_call(call_id="r1"):
    return {"id": call_id, "name": "list_directory", "args": {"dir_path": "/x"}, "status": "success"}


def _transcript(vault, calls):
    p = vault / "t.jsonl"
    rows = [{"sessionId": "s", "kind": "main"}]
    if calls:
        rows.append({"type": "gemini", "content": "", "toolCalls": calls})
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _run(monkeypatch, capsys, event):
    payload = json.dumps(event).encode("utf-8")
    monkeypatch.setattr(ga.sys, "stdin", io.StringIO(""))          # placeholder
    monkeypatch.setattr(ga.sys.stdin, "buffer", io.BytesIO(payload), raising=False)
    rc = ga.main()
    return rc, json.loads(capsys.readouterr().out)


# --- code-edit based eligibility (T09/T10) ---------------------------------------

def test_uncaptured_code_edit_emits_system_message(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py")])
    rc, out = _run(monkeypatch, capsys,
                   {"session_id": "g1", "transcript_path": str(t), "cwd": str(proj)})
    assert rc == 0
    assert "systemMessage" in out and "Singularity" in out["systemMessage"]
    assert "decision" not in out                     # headless-safe: never a retry


def test_nudge_is_once_per_session(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py")])
    event = {"session_id": "g2", "transcript_path": str(t), "cwd": str(proj)}
    _run(monkeypatch, capsys, event)
    _, out2 = _run(monkeypatch, capsys, event)
    assert out2 == {}                                # marker suppresses the second


def test_read_only_session_is_silent(vault, proj, monkeypatch, capsys):
    """No raw tool-count threshold (T09 acceptance) — a session with only
    read-type calls is silent regardless of how many there were."""
    t = _transcript(vault, [_read_call(f"r{i}") for i in range(20)])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g3", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_no_tool_calls_at_all_is_silent(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g3b", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_failed_edit_does_not_count(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py", status="error")])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g_fail", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_already_captured_via_sanctioned_command_is_silent(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py"),
                            _shell_call('mb learn "x" --task T')])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g4", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_already_captured_via_vault_write_is_silent(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py"),
                            _write_call(vault / "learnings" / "active" / "T.md", call_id="w2")])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g5", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_edit_inside_vault_alone_is_not_a_code_edit(vault, proj, monkeypatch, capsys):
    # a vault-only write with no OTHER code edit -> code_edits stays 0 -> silent
    t = _transcript(vault, [_write_call(vault / "learnings" / "active" / "T.md")])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g6", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_unrelated_command_without_mb_prefix_is_not_a_capture(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py"), _shell_call("npm test")])
    rc, out = _run(monkeypatch, capsys,
                   {"session_id": "g7", "transcript_path": str(t), "cwd": str(proj)})
    assert "systemMessage" in out       # still nudges — npm test isn't a capture


# --- guard cases ----------------------------------------------------------------

def test_auto_capture_disabled_is_silent(vault, proj, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text('{"auto_capture": false}', encoding="utf-8")
    t = _transcript(vault, [_write_call(proj / "src" / "app.py")])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g8", "transcript_path": str(t), "cwd": str(proj)})
    assert out == {}


def test_stop_hook_active_is_silent(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [_write_call(proj / "src" / "app.py")])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g9", "transcript_path": str(t), "cwd": str(proj),
                   "stop_hook_active": True})
    assert out == {}


def test_malformed_stdin_fails_open(vault, monkeypatch, capsys):
    monkeypatch.setattr(ga.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(ga.sys.stdin, "buffer", io.BytesIO(b"not json {{{"), raising=False)
    rc = ga.main()
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_missing_transcript_is_silent(vault, proj, monkeypatch, capsys):
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g10", "transcript_path": str(vault / "nope.jsonl"),
                   "cwd": str(proj)})
    assert out == {}


def test_claude_style_tool_use_line_does_not_count(vault, proj, monkeypatch, capsys):
    """A Claude-style `"tool_use"` line (different transcript shape entirely)
    must NOT be picked up by Gemini's `type == "gemini"` filter."""
    p = vault / "t.jsonl"
    p.write_text('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Edit"}]}}',
                 encoding="utf-8")
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g11", "transcript_path": str(p), "cwd": str(proj)})
    assert out == {}


# --- parse_transcript() unit-level: unrecognized path arg ---------------------------

def test_unrecognized_path_arg_warns_and_does_not_count(vault, proj):
    t = _transcript(vault, [{"id": "w1", "name": "write_file",
                             "args": {"nonstandard_key": "x"}, "status": "success"}])
    activity = ga.parse_transcript(str(t), "g12", str(proj), vault)
    assert activity.code_edits == 0
    assert activity.parse_warnings
