"""T10 differential tests: Claude and Gemini share the exact same
SessionActivity/decide() core (T09) — the SAME normalized activity must
yield the SAME eligibility on both clients. Only the wire action differs by
design (Claude: `decision:block`; Gemini: passive `systemMessage`), which
this file asserts is the ONLY intentional difference.
"""
import io
import json

import pytest

from singularity.hooks import gemini_after_agent as ga
from singularity.hooks import stop_capture as sc


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


def _run_claude(monkeypatch, capsys, event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.setattr(sc.sys.stdin, "buffer",
                        io.BytesIO(json.dumps(event).encode("utf-8")), raising=False)
    rc = sc.main()
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else {})


def _run_gemini(monkeypatch, capsys, event):
    monkeypatch.setattr(ga.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(ga.sys.stdin, "buffer",
                        io.BytesIO(json.dumps(event).encode("utf-8")), raising=False)
    rc = ga.main()
    return rc, json.loads(capsys.readouterr().out)


def _claude_transcript(vault, edit_path=None, capture_cmd=None):
    lines = []
    if edit_path:
        lines += [json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Edit", "input": {"file_path": str(edit_path)}}]}}),
                 json.dumps({"type": "user", "message": {"content": [
                     {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]}})]
    if capture_cmd:
        lines += [json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": capture_cmd}}]}}),
                 json.dumps({"type": "user", "message": {"content": [
                     {"type": "tool_result", "tool_use_id": "b1", "content": "ok"}]}})]
    p = vault / "claude.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _gemini_transcript(vault, edit_path=None, capture_cmd=None):
    calls = []
    if edit_path:
        calls.append({"id": "w1", "name": "write_file",
                      "args": {"file_path": str(edit_path), "content": "x"}, "status": "success"})
    if capture_cmd:
        calls.append({"id": "s1", "name": "run_shell_command",
                      "args": {"command": capture_cmd}, "status": "success"})
    p = vault / "gemini.jsonl"
    rows = [{"sessionId": "s", "kind": "main"}]
    if calls:
        rows.append({"type": "gemini", "content": "", "toolCalls": calls})
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


# --- differential: same activity -> same eligibility, different wire action -------

def test_uncaptured_edit_eligible_on_both_different_wire_action(vault, proj, monkeypatch, capsys):
    ct = _claude_transcript(vault, edit_path=proj / "app.py")
    _, claude_out = _run_claude(monkeypatch, capsys,
                                {"session_id": "diff1", "transcript_path": str(ct), "cwd": str(proj)})
    gt = _gemini_transcript(vault, edit_path=proj / "app.py")
    _, gemini_out = _run_gemini(monkeypatch, capsys,
                                {"session_id": "diff1g", "transcript_path": str(gt), "cwd": str(proj)})

    assert claude_out.get("decision") == "block"
    assert "systemMessage" in gemini_out
    # the ONLY intentional difference: wire action shape
    assert "systemMessage" not in claude_out
    assert "decision" not in gemini_out


def test_read_only_ineligible_on_both(vault, proj, monkeypatch, capsys):
    ct = _claude_transcript(vault)
    _, claude_out = _run_claude(monkeypatch, capsys,
                                {"session_id": "diff2", "transcript_path": str(ct), "cwd": str(proj)})
    gt = _gemini_transcript(vault)
    _, gemini_out = _run_gemini(monkeypatch, capsys,
                                {"session_id": "diff2g", "transcript_path": str(gt), "cwd": str(proj)})
    assert claude_out == {}
    assert gemini_out == {}


def test_sanctioned_capture_suppresses_both(vault, proj, monkeypatch, capsys):
    ct = _claude_transcript(vault, edit_path=proj / "app.py", capture_cmd='mb learn "x" --task T')
    _, claude_out = _run_claude(monkeypatch, capsys,
                                {"session_id": "diff3", "transcript_path": str(ct), "cwd": str(proj)})
    gt = _gemini_transcript(vault, edit_path=proj / "app.py", capture_cmd='mb learn "x" --task T')
    _, gemini_out = _run_gemini(monkeypatch, capsys,
                                {"session_id": "diff3g", "transcript_path": str(gt), "cwd": str(proj)})
    assert claude_out == {}
    assert gemini_out == {}


def test_disable_silences_both(vault, proj, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text('{"auto_capture": false}', encoding="utf-8")
    ct = _claude_transcript(vault, edit_path=proj / "app.py")
    _, claude_out = _run_claude(monkeypatch, capsys,
                                {"session_id": "diff4", "transcript_path": str(ct), "cwd": str(proj)})
    gt = _gemini_transcript(vault, edit_path=proj / "app.py")
    _, gemini_out = _run_gemini(monkeypatch, capsys,
                                {"session_id": "diff4g", "transcript_path": str(gt), "cwd": str(proj)})
    assert claude_out == {}
    assert gemini_out == {}


def test_vault_write_alone_ineligible_on_both(vault, proj, monkeypatch, capsys):
    ct = _claude_transcript(vault, edit_path=vault / "learnings" / "active" / "T.md")
    _, claude_out = _run_claude(monkeypatch, capsys,
                                {"session_id": "diff5", "transcript_path": str(ct), "cwd": str(proj)})
    gt = _gemini_transcript(vault, edit_path=vault / "learnings" / "active" / "T.md")
    _, gemini_out = _run_gemini(monkeypatch, capsys,
                                {"session_id": "diff5g", "transcript_path": str(gt), "cwd": str(proj)})
    assert claude_out == {}
    assert gemini_out == {}


# --- SessionStart is unaffected (T10 acceptance) -----------------------------------

def test_session_start_does_not_import_or_check_auto_capture():
    import inspect

    from singularity.hooks import session_start
    src = inspect.getsource(session_start)
    assert "auto_capture" not in src
    assert "capture_config_state" not in src
