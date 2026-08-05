"""GCLI-00 contract tests: pin the Gemini CLI hook / transcript / headless schema.

These validate the redacted fixtures captured from a real isolated run of
`@google/gemini-cli` 0.50.0 (see tests/fixtures/gemini/README.md). They are the
regression oracle for GCLI-01..07: if a future Gemini version drifts, re-capture
the fixtures and these assertions flag exactly what changed.

Pure fixture-schema tests — no dependency on register_gemini (not built yet).
"""
import json
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "gemini"

BASE_FIELDS = {"session_id", "transcript_path", "cwd", "hook_event_name", "timestamp"}
# The one thing that is NOT there — the auto-capture blocker.
FORBIDDEN_DISCRIMINATORS = {"interactive", "headless", "is_interactive", "mode", "non_interactive"}


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "fixture,event,extra",
    [
        ("hook_session_start.json", "SessionStart", {"source"}),
        ("hook_before_agent.json", "BeforeAgent", {"prompt"}),
        ("hook_after_agent_first.json", "AfterAgent", {"prompt", "prompt_response", "stop_hook_active"}),
        ("hook_after_agent_retry.json", "AfterAgent", {"prompt", "prompt_response", "stop_hook_active"}),
        ("hook_session_end.json", "SessionEnd", {"reason"}),
    ],
)
def test_hook_base_and_event_fields(fixture, event, extra):
    payload = _load(fixture)
    assert set(payload) >= BASE_FIELDS, f"{fixture} missing base fields"
    assert payload["hook_event_name"] == event
    assert extra <= set(payload), f"{fixture} missing event fields {extra}"


@pytest.mark.parametrize(
    "fixture",
    [
        "hook_session_start.json",
        "hook_before_agent.json",
        "hook_after_agent_first.json",
        "hook_after_agent_retry.json",
        "hook_session_end.json",
    ],
)
def test_no_headless_discriminator(fixture):
    """The core GCLI-00 finding: no payload reveals interactive vs headless."""
    payload = _load(fixture)
    assert FORBIDDEN_DISCRIMINATORS.isdisjoint(payload), (
        f"{fixture} unexpectedly exposes a mode discriminator; auto-capture design may be revisitable"
    )


def test_after_agent_retry_state_machine():
    """Exactly-once retry gate: first pass false, retry pass true."""
    assert _load("hook_after_agent_first.json")["stop_hook_active"] is False
    assert _load("hook_after_agent_retry.json")["stop_hook_active"] is True


def test_session_start_source_is_startup_or_resume_or_clear():
    assert _load("hook_session_start.json")["source"] in {"startup", "resume", "clear"}


def test_session_end_reason_enum():
    assert _load("hook_session_end.json")["reason"] in {
        "exit", "clear", "logout", "prompt_input_exit", "other",
    }


def test_headless_clean_has_no_warnings():
    """systemMessage / allow path: clean single-turn response, no warnings."""
    out = _load("headless_json_clean.json")
    assert {"session_id", "response", "stats"} <= set(out)
    assert "warnings" not in out


def test_headless_deny_mutates_output():
    """Regression guard for the NO-GO: deny concatenates turns AND adds warnings."""
    out = _load("headless_json_deny_mutation.json")
    assert "warnings" in out and out["warnings"], "deny must surface warnings[]"
    # concatenation signature: both turn texts present in the single response field
    assert "First turn reply" in out["response"] and "Retry turn reply" in out["response"]


def test_transcript_is_event_sourced_jsonl():
    lines = [ln for ln in (FIX / "transcript_sample.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    rows = [json.loads(ln) for ln in lines]
    # line 1 = session meta
    assert {"sessionId", "projectHash", "startTime", "kind"} <= set(rows[0])
    # at least one $set mutation and both message types present
    assert any("$set" in r for r in rows), "expected $set partial-update rows"
    types = {r.get("type") for r in rows if "type" in r}
    assert "user" in types and "gemini" in types, "expected user + gemini message types"


def test_transcript_tooluse_representation():
    """GCLI-04 parser MUST read `toolCalls` on a gemini message, NOT a 'tool_use' string."""
    rows = [json.loads(ln) for ln in (FIX / "transcript_tooluse.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    # no top-level line is typed "tool_use" — the plan's explicit warning
    assert all(r.get("type") != "tool_use" for r in rows)
    gem_with_tools = [r for r in rows if r.get("type") == "gemini" and r.get("toolCalls")]
    assert gem_with_tools, "expected a gemini message carrying toolCalls[]"
    call = gem_with_tools[0]["toolCalls"][0]
    assert {"id", "name", "args", "status"} <= set(call)
    # tool result is also echoed as a user message with functionResponse content
    fr = [r for r in rows if r.get("type") == "user"
          and isinstance(r.get("content"), list)
          and any("functionResponse" in c for c in r["content"])]
    assert fr, "expected user message with functionResponse (tool result echo)"


def test_before_after_tool_hook_fields():
    bt = _load("hook_before_tool.json")
    assert bt["hook_event_name"] == "BeforeTool"
    assert {"tool_name", "tool_input"} <= set(bt)
    at = _load("hook_after_tool.json")
    assert at["hook_event_name"] == "AfterTool"
    assert {"tool_name", "tool_input", "tool_response"} <= set(at)


def test_stream_json_event_sequence():
    rows = [json.loads(ln) for ln in (FIX / "stream_json_events.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    seq = [r["type"] for r in rows]
    assert seq[0] == "init" and seq[-1] == "result"
    assert "tool_use" in seq and "tool_result" in seq
