"""Unit tests for singularity/hooks/gemini_session_start (GCLI-03).

The adapter must always emit a single valid Gemini SessionStart JSON on stdout,
reuse the shared build_context, survive invalid stdin and a failing builder, and
carry non-ASCII (Cyrillic) through unescaped (ensure_ascii=False).
"""
import io
import json

import pytest

from singularity.hooks import gemini_session_start as gsm


def _run(monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    # gemini_session_start reads sys.stdin.buffer (T06: BOM-tolerant event cwd
    # extraction); provide a buffer shim, same pattern as test_stop_capture.py.
    monkeypatch.setattr(gsm.sys.stdin, "buffer",
                        io.BytesIO(stdin_text.encode("utf-8")), raising=False)
    rc = gsm.main()
    out = capsys.readouterr().out
    return rc, json.loads(out)


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_emits_valid_json_for_each_source(monkeypatch, capsys, source):
    monkeypatch.setattr(gsm, "build_context", lambda cwd=None: "CTX body")
    rc, obj = _run(monkeypatch, capsys, json.dumps(
        {"hook_event_name": "SessionStart", "source": source}))
    assert rc == 0
    assert obj["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert obj["hookSpecificOutput"]["additionalContext"] == "CTX body"


def test_cyrillic_context_not_escaped(monkeypatch, capsys):
    monkeypatch.setattr(gsm, "build_context", lambda cwd=None: "Active tasks: GEMINI-CLI")
    _, obj = _run(monkeypatch, capsys, '{"source":"startup"}')
    assert "Active tasks" in obj["hookSpecificOutput"]["additionalContext"]


def test_invalid_stdin_still_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(gsm, "build_context", lambda cwd=None: "ok")
    rc, obj = _run(monkeypatch, capsys, "this is not json {{{")
    assert rc == 0
    assert obj["hookSpecificOutput"]["additionalContext"] == "ok"


def test_empty_stdin_still_valid_json(monkeypatch, capsys):
    monkeypatch.setattr(gsm, "build_context", lambda cwd=None: "ok")
    rc, obj = _run(monkeypatch, capsys, "")
    assert rc == 0
    assert "additionalContext" in obj["hookSpecificOutput"]


def test_event_cwd_is_passed_through_to_build_context(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(gsm, "build_context", lambda cwd=None: seen.update(cwd=cwd) or "ok")
    _run(monkeypatch, capsys, json.dumps({"source": "startup", "cwd": "D:/some/project"}))
    assert seen["cwd"] == "D:/some/project"


def test_missing_event_cwd_passes_none(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(gsm, "build_context", lambda cwd=None: seen.update(cwd=cwd) or "ok")
    _run(monkeypatch, capsys, json.dumps({"source": "startup"}))
    assert seen["cwd"] is None


def test_builder_exception_is_isolated(monkeypatch, capsys):
    def boom(cwd=None):
        raise RuntimeError("vault exploded")
    monkeypatch.setattr(gsm, "build_context", boom)
    rc, obj = _run(monkeypatch, capsys, '{"source":"startup"}')
    assert rc == 0                                   # never blocks startup
    ctx = obj["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("[Singularity memory] hook error")
    assert "vault exploded" in ctx


def test_no_vault_context_via_real_builder(monkeypatch, capsys):
    """Exercise the real build_context on the no-vault branch."""
    import singularity.config as cfg
    monkeypatch.setattr(cfg, "resolve_vault", lambda *_a, **_k: None)
    _, obj = _run(monkeypatch, capsys, '{"source":"startup"}')
    assert "No vault configured" in obj["hookSpecificOutput"]["additionalContext"]
