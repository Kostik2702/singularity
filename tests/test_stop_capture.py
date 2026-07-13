"""Unit tests for the Claude Stop auto-capture hook (T10, replaces the MB-09
tool-count version).

Fixtures use the REAL Claude Code transcript JSONL shape, verified against a
live transcript on 2026-07-12 (Anthropic documents this shape as internal/
version-unstable, hence the deliberately conservative parser under test):

  {"type": "assistant", "message": {"content": [
      {"type": "tool_use", "id": "toolu_x", "name": "Edit",
       "input": {"file_path": "...", ...}}]}}
  {"type": "user", "message": {"content": [
      {"type": "tool_result", "tool_use_id": "toolu_x", "content": "...",
       "is_error": true}]}}   # is_error key ABSENT (not false) on success

Decision is now code-edit based (T09's SessionActivity/decide()), not a raw
tool-count threshold.
"""
import io
import json

import pytest

from singularity.hooks import stop_capture


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


def _tool_use(tid, name, tool_input):
    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tid, "name": name, "input": tool_input}]}})


def _tool_result(tid, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
    if is_error:
        block["is_error"] = True
    return json.dumps({"type": "user", "message": {"content": [block]}})


def _edit(abs_path, tid="t1", is_error=False):
    return [_tool_use(tid, "Edit", {"file_path": str(abs_path)}), _tool_result(tid, is_error)]


def _bash(command, tid="b1"):
    return [_tool_use(tid, "Bash", {"command": command}), _tool_result(tid)]


def _powershell(command, tid="p1"):
    return [_tool_use(tid, "PowerShell", {"command": command}), _tool_result(tid)]


def _read(tid="r1"):
    return [_tool_use(tid, "Read", {"file_path": "/x"}), _tool_result(tid)]


def _transcript(vault, lines):
    p = vault / "t.jsonl"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _run(monkeypatch, capsys, event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    # stop_capture reads sys.stdin.buffer; provide a buffer shim
    monkeypatch.setattr(stop_capture.sys.stdin, "buffer",
                        io.BytesIO(json.dumps(event).encode("utf-8")), raising=False)
    rc = stop_capture.main()
    return rc, capsys.readouterr().out


# --- code-edit based eligibility (T09/T10) ---------------------------------------

def test_uncaptured_code_edit_blocks_once(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, _edit(proj / "src" / "app.py"))
    rc, out = _run(monkeypatch, capsys,
                   {"session_id": "s1", "transcript_path": str(t), "cwd": str(proj)})
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    # marker now exists -> a second run allows
    rc2, out2 = _run(monkeypatch, capsys,
                     {"session_id": "s1", "transcript_path": str(t), "cwd": str(proj)})
    assert out2.strip() == ""


def test_read_only_session_allows(vault, proj, monkeypatch, capsys):
    """No raw tool-count threshold (T09 acceptance) — any number of read-only
    calls never triggers."""
    lines = []
    for i in range(20):
        lines.extend(_read(f"r{i}"))
    t = _transcript(vault, lines)
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s2", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_no_tool_calls_at_all_allows(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, [])
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s2b", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_failed_edit_does_not_count(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, _edit(proj / "src" / "app.py", is_error=True))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s_fail", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_already_captured_via_mb_learn_allows(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, _edit(proj / "src" / "app.py")
                    + _bash('mb learn "x" --task T', tid="b1"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s3", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_already_captured_via_powershell_mb_learn_allows(vault, proj, monkeypatch, capsys):
    """Real-machine finding (2026-07-12, this session's own transcript):
    Windows Claude Code uses the "PowerShell" tool name for shell commands
    at least as often as "Bash" — every real `mb learn` call in this actual
    session went through PowerShell. A Bash-only check silently missed all
    of them until this was caught by inspecting the live transcript."""
    t = _transcript(vault, _edit(proj / "src" / "app.py")
                    + _powershell('Set-Location "D:\\Singularity"; mb learn "x" --task T'))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s3b", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_already_captured_via_direct_vault_write_allows(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, _edit(proj / "src" / "app.py", tid="t1")
                    + _edit(vault / "learnings" / "active" / "T.md", tid="t2"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s4", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_edit_inside_vault_alone_is_not_a_code_edit(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, _edit(vault / "learnings" / "active" / "T.md"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s5", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_unrelated_working_tree_dirt_irrelevant(vault, proj, monkeypatch, capsys):
    """Only THIS session's confirmed edits count — an unrelated Bash command
    that merely touches files on disk (not via Edit/Write) is not tracked."""
    t = _transcript(vault, _bash("touch some_file.txt", tid="b1"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s6", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_edit_outside_cwd_does_not_count(vault, proj, monkeypatch, capsys, tmp_path):
    other = tmp_path / "elsewhere"; other.mkdir()
    t = _transcript(vault, _edit(other / "x.py"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s7", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


# --- guard cases ----------------------------------------------------------------

def test_stop_hook_active_allows(vault, proj, monkeypatch, capsys):
    t = _transcript(vault, _edit(proj / "src" / "app.py"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s8", "transcript_path": str(t), "cwd": str(proj),
                   "stop_hook_active": True})
    assert out.strip() == ""


def test_auto_capture_disabled_allows(vault, proj, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text('{"auto_capture": false}', encoding="utf-8")
    t = _transcript(vault, _edit(proj / "src" / "app.py"))
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s9", "transcript_path": str(t), "cwd": str(proj)})
    assert out.strip() == ""


def test_malformed_stdin_fails_open(vault, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json {{{"))
    monkeypatch.setattr(stop_capture.sys.stdin, "buffer",
                        io.BytesIO(b"not json {{{"), raising=False)
    rc = stop_capture.main()
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_missing_transcript_allows(vault, proj, monkeypatch, capsys):
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s10", "transcript_path": str(vault / "nope.jsonl"),
                   "cwd": str(proj)})
    assert out.strip() == ""


def test_malformed_config_fails_open_not_a_crash(vault, proj, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text("{not json", encoding="utf-8")
    t = _transcript(vault, _edit(proj / "src" / "app.py"))
    rc, out = _run(monkeypatch, capsys,
                   {"session_id": "s11", "transcript_path": str(t), "cwd": str(proj)})
    assert rc == 0     # source=error -> capture_config_state defaults configured=True
    assert json.loads(out)["decision"] == "block"


# --- parse_transcript() unit-level -------------------------------------------------

def test_parse_transcript_multiedit_also_counts(vault, proj):
    t = _transcript(vault, [_tool_use("m1", "MultiEdit", {"file_path": str(proj / "x.py")}),
                            _tool_result("m1")])
    activity = stop_capture.parse_transcript(str(t), "s12", str(proj), vault)
    assert activity.code_edits == 1


def test_parse_transcript_missing_file_path_warns(vault, proj):
    t = _transcript(vault, [_tool_use("t1", "Edit", {}), _tool_result("t1")])
    activity = stop_capture.parse_transcript(str(t), "s13", str(proj), vault)
    assert activity.code_edits == 0
    assert activity.parse_warnings
