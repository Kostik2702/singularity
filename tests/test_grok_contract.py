"""GRK-00 contract tests: pin the Grok Build CLI hook / session / headless schema.

These validate the redacted fixtures captured from a real isolated run of
Grok Build CLI 0.2.111 on Windows (see tests/fixtures/grok/README.md). They are
the regression oracle for GRK-01..07: if a future Grok version drifts,
re-capture with tools/grok_contract_probe.py and these assertions flag exactly
what changed.

Pure fixture-schema tests — no dependency on register_grok (not built yet).
"""
import json
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parent / "fixtures" / "grok"

MIN_GROK_VERSION = "0.2.111"

# Every hook payload carries this camelCase envelope (grok is camelCase where
# Claude Code is snake_case — the GRK-04 adapter must translate).
BASE_FIELDS = {"hookEventName", "sessionId", "cwd", "workspaceRoot", "timestamp", "permissionMode"}
# Claude-style snake_case keys that must NOT appear at the top level: their
# presence would mean grok changed its envelope and the adapter can simplify.
CLAUDE_SNAKE_KEYS = {"hook_event_name", "session_id", "stop_hook_active", "tool_response"}


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _jsonl(name):
    lines = (FIX / name).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.mark.parametrize(
    "fixture,event,extra",
    [
        ("hook_session_start.json", "session_start", {"source"}),
        ("hook_stop.json", "stop", {"transcriptPath", "promptId", "reason",
                                    "stopHookActive", "lastAssistantMessage",
                                    "backgroundTasks", "sessionCrons"}),
        ("hook_pre_tool_use.json", "pre_tool_use", {"toolName", "toolUseId",
                                                    "toolInput", "toolInputTruncated"}),
        ("hook_post_tool_use.json", "post_tool_use", {"toolName", "toolUseId",
                                                      "toolInput", "toolResult",
                                                      "toolResultTruncated"}),
    ],
)
def test_hook_base_and_event_fields(fixture, event, extra):
    payload = _load(fixture)
    assert BASE_FIELDS <= set(payload), f"{fixture} missing base fields"
    assert payload["hookEventName"] == event
    assert extra <= set(payload), f"{fixture} missing event fields {extra}"


@pytest.mark.parametrize(
    "fixture",
    ["hook_session_start.json", "hook_stop.json",
     "hook_pre_tool_use.json", "hook_post_tool_use.json"],
)
def test_hook_envelope_is_camel_case(fixture):
    """Core GRK-00 finding: grok speaks camelCase; Claude hook scripts reading
    snake_case keys silently no-op. The adapter must read BOTH."""
    payload = _load(fixture)
    assert CLAUDE_SNAKE_KEYS.isdisjoint(payload), (
        f"{fixture} grew Claude-style snake_case keys; adapter dual-read may be droppable"
    )


def test_stop_payload_gate_semantics():
    """Stop gate contract used by the GRK-04 capture nudge."""
    stop = _load("hook_stop.json")
    assert stop["reason"] == "end_turn", "gate must filter on end_turn (session-end fire uses other reasons)"
    assert stop["stopHookActive"] is False
    assert isinstance(stop["backgroundTasks"], list)
    assert isinstance(stop["sessionCrons"], list)
    # transcriptPath points at the session's updates.jsonl inside the sessions tree
    assert stop["transcriptPath"].endswith("updates.jsonl")
    assert "sessions" in stop["transcriptPath"]


def test_post_tool_use_result_contract():
    """GRK-05 / PostToolUse consumers read toolResult (NOT Claude's tool_response)."""
    post = _load("hook_post_tool_use.json")
    result = post["toolResult"]
    assert {"output_for_prompt", "exit_code", "command"} <= set(result)
    assert result["exit_code"] == 0
    assert "tool_response" not in post


def test_hook_env_vars_contract():
    env = _load("hook_env_vars.json")
    assert {"GROK_HOOK_EVENT", "GROK_HOOK_NAME", "GROK_SESSION_ID",
            "GROK_WORKSPACE_ROOT", "CLAUDE_PROJECT_DIR"} <= set(env)
    # CLAUDE_PROJECT_DIR is the Claude-compat alias of GROK_WORKSPACE_ROOT
    assert env["CLAUDE_PROJECT_DIR"] == env["GROK_WORKSPACE_ROOT"]


def test_headless_json_result_schema():
    out = _load("headless_json_result.json")
    assert {"text", "stopReason", "sessionId", "requestId", "usage", "num_turns",
            "modelUsage"} <= set(out)
    usage = out["usage"]
    # snake_case token fields; input_tokens is UNCACHED-only per the headless token policy
    assert {"input_tokens", "cache_read_input_tokens", "output_tokens",
            "total_tokens"} <= set(usage)
    assert usage["total_tokens"] == (usage["input_tokens"]
                                     + usage["cache_read_input_tokens"]
                                     + usage["output_tokens"])


def test_streaming_json_events():
    rows = _jsonl("stream_json_events.jsonl")
    assert all("type" in r for r in rows)
    assert rows[-1]["type"] == "end", "end must be the last event"
    assert {"stopReason", "sessionId", "usage"} <= set(rows[-1])


def test_session_summary_toplevel_has_no_subagent_kind():
    summary = _load("session_summary_toplevel.json")
    assert {"info", "created_at", "updated_at", "num_messages",
            "current_model_id", "chat_format_version"} <= set(summary)
    assert "session_kind" not in summary, (
        "top-level sessions carry no session_kind; GRK-05 keeps them"
    )


def test_session_summary_subagent_marker():
    """GRK-05 subagent exclusion: child summary.json carries session_kind."""
    summary = _load("session_summary_subagent.json")
    assert summary["session_kind"] == "subagent"


def test_subagent_meta_contract():
    meta = _load("subagent_meta.json")
    assert {"subagent_id", "parent_session_id", "child_session_id",
            "subagent_type", "status", "turns"} <= set(meta)
    assert meta["subagent_id"] == meta["child_session_id"]
    assert meta["status"] in {"completed", "running", "failed", "cancelled"}


def test_chat_history_row_types_and_prompt_markers():
    """GRK-05 parser contract for chat_history.jsonl."""
    rows = _jsonl("chat_history_tooluse.jsonl")
    types = {r["type"] for r in rows}
    assert types == {"system", "user", "assistant", "tool_result"}
    # exactly the real user prompt carries prompt_index
    real_prompts = [r for r in rows if "prompt_index" in r]
    assert len(real_prompts) == 1 and real_prompts[0]["type"] == "user"
    # injected context is discriminated by synthetic_reason
    synthetic = {r["synthetic_reason"] for r in rows if "synthetic_reason" in r}
    assert synthetic <= {"project_instructions", "system_reminder"}
    # tool call representation: assistant row with tool_calls, echoed as tool_result row
    calls = [r for r in rows if r["type"] == "assistant" and r.get("tool_calls")]
    assert calls, "expected an assistant row carrying tool_calls[]"
    call = calls[0]["tool_calls"][0]
    assert {"id", "name", "arguments"} <= set(call)
    results = [r for r in rows if r["type"] == "tool_result"]
    assert results and results[0]["tool_call_id"] == call["id"]


def test_edit_tool_result_failure_signatures():
    """GRK-04 parser contract: tool errors carry fixed content prefixes
    ('Tool `x` failed: ...' / 'Failed to parse arguments...'); a successful
    write reads as free text WITHOUT those prefixes. The capture adapter's
    success/failure classifier is built on exactly these signatures."""
    rows = _jsonl("chat_history_edit_results.jsonl")
    results = {r["tool_call_id"]: r["content"] for r in rows if r["type"] == "tool_result"}
    ok = results["call_762af7f0"]
    io_fail = results["call_ed8d8b5a"]
    arg_fail = results["call_a9a10bad"]
    assert not ok.startswith("Tool `") and not ok.startswith("Failed to parse arguments")
    assert io_fail.startswith("Tool `") and "` failed:" in io_fail
    assert arg_fail.startswith("Failed to parse arguments")


def test_windows_hook_example_never_uses_bare_quoted_argv0():
    """Regression guard for the Windows quoting kill-finding: a hook command
    starting with a double-quoted path (Claude settings.json style) fails under
    grok's PowerShell hook runner. Safe forms: unquoted path or `& "..."`."""
    doc = _load("hooks_windows_example.json")
    for groups in doc["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                cmd = hook["command"]
                assert not cmd.startswith('"'), f"bare quoted argv0 breaks on Windows: {cmd}"
                assert "$env:" not in cmd, "load-time $-expansion corrupts $env: references"
