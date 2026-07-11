"""Unit tests for the Gemini AfterAgent auto-capture nudge (GCLI-04).

Key safety property: the hook NEVER emits `decision` (deny) — only a passive
`systemMessage` — because GCLI-00 proved deny mutates headless `-o json`. All guard
cases emit an empty object and the nudge fires at most once per session.
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


def _transcript(vault, tool_calls=0, captured=False):
    p = vault / "t.jsonl"
    calls = []
    for i in range(tool_calls):
        if captured and i == 0:
            calls.append({"id": f"c{i}", "name": "run_shell_command",
                          "args": {"command": 'mb learn "x" --task T'}, "status": "success"})
        else:
            calls.append({"id": f"c{i}", "name": "list_directory",
                          "args": {"dir_path": "/x"}, "status": "success"})
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


from singularity.hooks.capture_decision import MIN_TOOL_USES


def test_substantial_uncaptured_emits_system_message(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_calls=MIN_TOOL_USES)
    rc, out = _run(monkeypatch, capsys, {"session_id": "g1", "transcript_path": str(t)})
    assert rc == 0
    assert "systemMessage" in out and "Singularity" in out["systemMessage"]
    assert "decision" not in out                     # headless-safe: never a retry


def test_nudge_is_once_per_session(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_calls=MIN_TOOL_USES)
    _run(monkeypatch, capsys, {"session_id": "g2", "transcript_path": str(t)})
    _, out2 = _run(monkeypatch, capsys, {"session_id": "g2", "transcript_path": str(t)})
    assert out2 == {}                                # marker suppresses the second


def test_trivial_session_is_silent(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_calls=2)
    _, out = _run(monkeypatch, capsys, {"session_id": "g3", "transcript_path": str(t)})
    assert out == {}


def test_already_captured_is_silent(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_calls=MIN_TOOL_USES, captured=True)
    _, out = _run(monkeypatch, capsys, {"session_id": "g4", "transcript_path": str(t)})
    assert out == {}


def test_auto_capture_disabled_is_silent(vault, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text('{"auto_capture": false}', encoding="utf-8")
    t = _transcript(vault, tool_calls=MIN_TOOL_USES)
    _, out = _run(monkeypatch, capsys, {"session_id": "g5", "transcript_path": str(t)})
    assert out == {}


def test_stop_hook_active_is_silent(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_calls=MIN_TOOL_USES)
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g6", "transcript_path": str(t), "stop_hook_active": True})
    assert out == {}


def test_malformed_stdin_fails_open(vault, monkeypatch, capsys):
    monkeypatch.setattr(ga.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(ga.sys.stdin, "buffer", io.BytesIO(b"not json {{{"), raising=False)
    rc = ga.main()
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_missing_transcript_is_silent(vault, monkeypatch, capsys):
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "g7", "transcript_path": str(vault / "nope.jsonl")})
    assert out == {}


def test_tool_use_string_does_not_count(vault, monkeypatch, capsys):
    """A Claude-style `"tool_use"` line must NOT be counted (Gemini uses toolCalls)."""
    p = vault / "t.jsonl"
    p.write_text("\n".join('{"type":"tool_use","name":"Read"}' for _ in range(MIN_TOOL_USES)),
                 encoding="utf-8")
    _, out = _run(monkeypatch, capsys, {"session_id": "g8", "transcript_path": str(p)})
    assert out == {}                                 # zero toolCalls -> trivial
