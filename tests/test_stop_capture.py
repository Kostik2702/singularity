"""Characterization tests for the Claude Stop auto-capture hook (pre-GCLI-04).

There were no tests for stop_capture before; these pin its CURRENT observable
behaviour so the GCLI-04 extraction of shared decision logic cannot silently
change Claude semantics. The hook BLOCKS exactly once on a substantial,
un-captured session and allows (rc 0, no output) in every guard case.
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


def _transcript(vault, tool_uses=0, captured=False):
    p = vault / "t.jsonl"
    lines = []
    for i in range(tool_uses):
        if captured and i == 0:
            lines.append('{"type":"tool_use","name":"Bash","input":{"command":"mb learn \\"x\\" --task T"}}')
        else:
            lines.append('{"type":"tool_use","name":"Read"}')
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _run(monkeypatch, capsys, event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    # stop_capture reads sys.stdin.buffer; provide a buffer shim
    monkeypatch.setattr(stop_capture.sys.stdin, "buffer",
                        io.BytesIO(json.dumps(event).encode("utf-8")), raising=False)
    rc = stop_capture.main()
    return rc, capsys.readouterr().out


def test_substantial_uncaptured_blocks_once(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_uses=stop_capture.MIN_TOOL_USES)
    rc, out = _run(monkeypatch, capsys, {"session_id": "s1", "transcript_path": str(t)})
    assert rc == 0
    assert json.loads(out)["decision"] == "block"
    # marker now exists -> a second run allows
    rc2, out2 = _run(monkeypatch, capsys, {"session_id": "s1", "transcript_path": str(t)})
    assert out2.strip() == ""


def test_trivial_session_allows(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_uses=2)
    _, out = _run(monkeypatch, capsys, {"session_id": "s2", "transcript_path": str(t)})
    assert out.strip() == ""


def test_already_captured_allows(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_uses=stop_capture.MIN_TOOL_USES, captured=True)
    _, out = _run(monkeypatch, capsys, {"session_id": "s3", "transcript_path": str(t)})
    assert out.strip() == ""


def test_stop_hook_active_allows(vault, monkeypatch, capsys):
    t = _transcript(vault, tool_uses=stop_capture.MIN_TOOL_USES)
    _, out = _run(monkeypatch, capsys,
                  {"session_id": "s4", "transcript_path": str(t), "stop_hook_active": True})
    assert out.strip() == ""


def test_auto_capture_disabled_allows(vault, monkeypatch, capsys):
    (vault / ".mb" / "config.json").write_text('{"auto_capture": false}', encoding="utf-8")
    t = _transcript(vault, tool_uses=stop_capture.MIN_TOOL_USES)
    _, out = _run(monkeypatch, capsys, {"session_id": "s5", "transcript_path": str(t)})
    assert out.strip() == ""
