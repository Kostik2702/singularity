"""Per-step token accounting: the duplication trap, per-category pricing, and
the context-growth balance.

Every test here pins a property that a plausible-looking implementation gets
wrong SILENTLY — the failure mode is a number that looks right, not a crash:

* summing JSONL records instead of messages (~1.8x overstatement),
* reading only a message's first record (loses most tool calls),
* adding the input categories together (cache reads cost 0.1x, 1h writes 2x),
* attributing growth to tools only (two thirds of it comes from elsewhere).
"""
from __future__ import annotations

import json
import os

import pytest

from singularity import render
from singularity.telemetry import tokens


# --- fixture builders (the real wire shape, verified against live transcripts)


def _usage(inp=0, read=0, w5=0, w1h=0, out=0, speed="standard", legacy=None):
    """`legacy` reproduces an older transcript: a cache_creation total with no
    TTL split, which must NOT be priced at the expensive 1h rate."""
    block = {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": legacy if legacy is not None else w5 + w1h,
        "output_tokens": out,
        "speed": speed,
    }
    if legacy is None:
        block["cache_creation"] = {"ephemeral_5m_input_tokens": w5,
                                   "ephemeral_1h_input_tokens": w1h}
    return block


def _asst(mid, usage, blocks, model="claude-opus-5", sidechain=False):
    return {"type": "assistant", "isSidechain": sidechain,
            "timestamp": "2026-08-07T10:00:00Z",
            "message": {"id": mid, "model": model, "role": "assistant",
                        "content": blocks, "usage": usage}}


def _tool_use(name, tid, **payload):
    return {"type": "tool_use", "id": tid, "name": name, "input": payload}


def _result(tid, text, is_error=False):
    return {"type": "user", "isSidechain": False,
            "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid,
                 "content": text, "is_error": is_error}]}}


def _injection(text):
    return {"type": "attachment", "isSidechain": False,
            "attachment": {"type": "queued_command", "prompt": text}}


def _write(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records)
                    + "\n", encoding="utf-8")
    return path


# --- the duplication trap ----------------------------------------------------


def test_usage_is_counted_once_per_message_not_once_per_record(tmp_path):
    """Claude Code writes one record per content block and repeats the SAME
    usage in each. Three records, one bill."""
    usage = _usage(read=10_000, out=500)
    path = _write(tmp_path / "s.jsonl", [
        _asst("msg_1", usage, [{"type": "thinking", "thinking": "..."}]),
        _asst("msg_1", usage, [{"type": "text", "text": "hi"}]),
        _asst("msg_1", usage, [_tool_use("Read", "t1", file_path="/a.py")]),
    ])
    session = tokens.parse(path)
    assert len(session.turns) == 1, "three records of one message are one turn"
    turn = session.turns[0]
    assert turn.cache_read == 10_000, "the repeated usage was added up"
    assert turn.output_tokens == 500


def test_tool_use_is_read_from_every_record_not_just_the_first(tmp_path):
    """The second half of the same trap: the tool_use block is usually NOT in
    the message's first record. A reader that keeps the first record loses the
    work the turn actually did."""
    usage = _usage(read=1_000, out=10)
    path = _write(tmp_path / "s.jsonl", [
        _asst("msg_1", usage, [{"type": "thinking", "thinking": "..."}]),
        _asst("msg_1", usage, [_tool_use("Bash", "t1", command="ls")]),
        _asst("msg_1", usage, [_tool_use("Read", "t2", file_path="/a.py")]),
    ])
    session = tokens.parse(path)
    assert [c.name for c in session.turns[0].tools] == ["Bash", "Read"]


def test_a_disagreeing_repeat_is_reported_rather_than_silently_picked(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _asst("msg_1", _usage(read=1_000, out=10), [{"type": "text", "text": "a"}]),
        _asst("msg_1", _usage(read=9_999, out=10), [{"type": "text", "text": "b"}]),
    ])
    session = tokens.parse(path)
    assert session.turns[0].cache_read == 1_000
    assert any("DIFFERENT usage" in w for w in session.warnings)


# --- pricing -----------------------------------------------------------------


def test_each_input_category_is_priced_by_its_own_multiplier(tmp_path):
    """A single "input tokens" number is meaningless: a cache read costs 0.1x
    and a 1h cache write costs 2x of the same model's input rate."""
    path = _write(tmp_path / "s.jsonl", [
        _asst("msg_1", _usage(inp=1_000, read=1_000, w5=1_000, w1h=1_000, out=1_000),
              [{"type": "text", "text": "x"}]),
    ])
    turn = tokens.parse(path).turns[0]
    expected = ((1_000 + 1_000 * 0.1 + 1_000 * 1.25 + 1_000 * 2.0) * 5.0
                + 1_000 * 25.0) / 1_000_000
    assert turn.cost_usd == pytest.approx(expected)
    # The naive "add the input categories, price once" answer differs — the
    # test would not notice the mistake if it did not.
    naive = (4_000 * 5.0 + 1_000 * 25.0) / 1_000_000
    assert turn.cost_usd != pytest.approx(naive)


def test_fast_mode_is_read_from_the_field_never_assumed(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(out=1_000, speed="standard"), [{"type": "text", "text": "a"}]),
        _asst("m2", _usage(out=1_000, speed="fast"), [{"type": "text", "text": "b"}]),
    ])
    standard, fast = tokens.parse(path).turns
    assert standard.cost_usd == pytest.approx(1_000 * 25.0 / 1_000_000)
    assert fast.cost_usd == pytest.approx(1_000 * 50.0 / 1_000_000)


def test_an_unknown_model_reports_tokens_and_no_invented_price(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=5_000, out=100), [{"type": "text", "text": "a"}],
              model="claude-something-unreleased"),
    ])
    session = tokens.parse(path)
    assert session.turns[0].cache_read == 5_000, "tokens stay exact"
    assert session.turns[0].cost_usd is None, "no rate on file must mean no number"
    payload = tokens.summarize(session)
    assert payload["totals"]["cost_usd"] is None
    assert any("no published rate" in w for w in payload["warnings"])


def test_model_id_variants_price_as_the_same_model():
    """A context-window suffix or a platform prefix is a deployment variant,
    not a rate card."""
    for variant in ("claude-opus-5", "claude-opus-5[1m]", "anthropic.claude-opus-5"):
        assert tokens.rate_for(variant, "standard") == tokens.Rate(5.0, 25.0), variant


def test_legacy_cache_write_without_a_ttl_split_is_priced_cheap(tmp_path):
    """Old transcripts carry only a total. Guessing the expensive 1h rate would
    overstate the bill by 60% on exactly the sessions nobody can re-measure."""
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(legacy=1_000), [{"type": "text", "text": "a"}]),
    ])
    turn = tokens.parse(path).turns[0]
    assert turn.write_other == 1_000
    assert turn.cost_usd == pytest.approx(1_000 * 1.25 * 5.0 / 1_000_000)


# --- context growth ----------------------------------------------------------


def _growth_session(tmp_path):
    """Two steps of real growth: a tool result and a hook injection."""
    return _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=1_000, out=100),
              [_tool_use("Read", "t1", file_path="/big.py")]),
        _result("t1", "x" * 3_000),
        _injection("y" * 1_000),
        _asst("m2", _usage(read=2_000, out=50), [{"type": "text", "text": "done"}]),
        _asst("m3", _usage(read=2_300, out=10), [{"type": "text", "text": "bye"}]),
    ])


def test_growth_sources_add_up_to_the_measured_growth(tmp_path):
    """The balance property. An attribution that covers only tool calls looks
    like "tools are cheap" when the truth is that most growth came from
    somewhere it never looked."""
    payload = tokens.summarize(tokens.parse(_growth_session(tmp_path)))
    assert payload["context"]["grew"] > 0
    assert sum(r["added"] for r in payload["by_source"]) == payload["context"]["grew"]


def test_the_assistants_own_output_is_a_named_source(tmp_path):
    """It is usually the biggest one, and a report that hides it sends people
    hunting for a leak in their tools."""
    payload = tokens.summarize(tokens.parse(_growth_session(tmp_path)))
    labels = {r["label"] for r in payload["by_source"]}
    assert tokens.ASSISTANT in labels
    assert tokens.INJECTED in labels, "hook/system injections must be visible"
    assert "Read" in labels, "a tool result must be charged to its own call"


def test_prompt_size_delta_is_taken_from_reported_usage_not_estimated(tmp_path):
    path = _growth_session(tmp_path)
    turns = tokens.parse(path).turns
    assert turns[0].prefix == 1_000 and turns[1].prefix == 2_000
    assert turns[0].delta == 1_000, "growth is a difference of two reported sizes"
    assert turns[-1].delta is None, "the last turn has no next prompt to compare to"


def test_compaction_is_recorded_as_a_reset_not_as_negative_growth(tmp_path):
    """A shrinking prefix is normal (compaction, /clear). Clamping it to zero
    would hide the one event a leak hunt most wants to see."""
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=100_000, out=10), [{"type": "text", "text": "a"}]),
        _asst("m2", _usage(read=20_000, out=10), [{"type": "text", "text": "b"}]),
        _asst("m3", _usage(read=25_000, out=10), [{"type": "text", "text": "c"}]),
    ])
    session = tokens.parse(path)
    assert session.turns[0].reset is True
    assert session.reclaimed == 80_000
    payload = tokens.summarize(session)
    assert payload["context"]["resets"] == 1
    assert all(r["added"] >= 0 for r in payload["by_source"])


def test_a_client_side_error_row_is_not_a_turn(tmp_path):
    """`model: "<synthetic>"` rows ("API Error: Connection closed mid-response")
    carry an all-zero usage. Counted as a turn, one of them puts a prompt size
    of 0 between two real turns — which reads as a context reset AND an equally
    large regrowth on the next step. Measured on a real 553-turn session, that
    single row invented 580k of growth and charged it to whatever arrived next.
    """
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=100_000, out=10), [{"type": "text", "text": "a"}]),
        _asst("err", _usage(), [{"type": "text", "text": "API Error: Connection closed"}],
              model="<synthetic>"),
        _asst("m2", _usage(read=101_000, out=10), [{"type": "text", "text": "b"}]),
    ])
    session = tokens.parse(path)
    assert [t.message_id for t in session.turns] == ["m1", "m2"]
    assert session.reclaimed == 0, "a zero-usage row must not look like a compaction"
    assert session.turns[0].delta == 1_000, "growth must span the two real turns"
    assert any("client-side row" in w for w in session.warnings)


def test_hook_and_reminder_text_is_an_injection_not_user_input(tmp_path):
    """Both arrive in a `user` message; only one of them is the person typing.
    Filing hook output under "user input" hides the source the owner can
    actually turn off."""
    reminder = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
        {"type": "text", "text": "<system-reminder>" + "z" * 3_000 + "</system-reminder>"}]}}
    typed = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
        {"type": "text", "text": "q" * 1_000}]}}
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=1_000, out=0), [{"type": "text", "text": "a"}]),
        reminder, typed,
        _asst("m2", _usage(read=2_000, out=0), [{"type": "text", "text": "b"}]),
    ])
    payload = tokens.summarize(tokens.parse(path))
    by_label = {r["label"]: r["added"] for r in payload["by_source"]}
    assert by_label[tokens.INJECTED] > by_label[tokens.USER] > 0
    assert by_label[tokens.INJECTED] == pytest.approx(750, abs=5)   # 3000/4000 of 1000


def test_carry_stops_at_a_context_reset(tmp_path):
    """Tokens added before a compaction are not re-read after it. Charging them
    to the end of the session would invent cost that was never paid."""
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=1_000, out=0), [_tool_use("Read", "t1", file_path="/a")]),
        _result("t1", "x" * 4_000),
        _asst("m2", _usage(read=3_000, out=0), [{"type": "text", "text": "b"}]),
        _asst("m3", _usage(read=500, out=0), [{"type": "text", "text": "c"}]),
        _asst("m4", _usage(read=600, out=0), [{"type": "text", "text": "d"}]),
    ])
    session = tokens.parse(path)
    call = session.turns[0].tools[0]
    assert call.added > 0
    # turn 0 is followed by a reset at turn 1, so its tokens are carried
    # exactly once — not across the whole four-turn session.
    assert call.carry_tokens == call.added


def test_carry_prices_re_reading_not_the_turn_that_caused_it(tmp_path):
    """The point of the metric: a big read early in a long session costs far
    more than its own turn shows."""
    records = [_asst("m0", _usage(read=1_000, out=0),
                     [_tool_use("Read", "t1", file_path="/big")]),
               _result("t1", "x" * 10_000)]
    for i in range(1, 20):
        records.append(_asst(f"m{i}", _usage(read=5_000 + i, out=0),
                             [{"type": "text", "text": "."}]))
    session = tokens.parse(_write(tmp_path / "s.jsonl", records))
    call = session.turns[0].tools[0]
    assert call.carry_tokens == call.added * 19
    assert call.carry_usd == pytest.approx(call.carry_tokens * 5.0 * 0.1 / 1_000_000)


# --- subagents ---------------------------------------------------------------


def _delegating_session(tmp_path, *, synchronous: bool = False):
    """A parent that launched one subagent, plus the sidecar the client writes.

    `synchronous=True` reproduces a foreground `Agent` call: its tool_result is
    the agent's REPORT, with no launch envelope naming an agentId anywhere —
    the case that used to leave the delegation unidentified.
    """
    launch_result = ("Тут был длинный отчёт агента." if synchronous else
                     "Async agent launched successfully. agentId: abc123def456 (internal)")
    main = _write(tmp_path / "sess.jsonl", [
        _asst("m1", _usage(read=1_000, out=10),
              [_tool_use("Agent", "t1", subagent_type="reviewer")]),
        _result("t1", launch_result),
        _asst("m2", _usage(read=1_200, out=10), [{"type": "text", "text": "ok"}]),
    ])
    # The sidechain turn carries a 1h cache WRITE as well as reads: pricing it
    # exercises every category, so a second copy of the cost formula (or a
    # drifting multiplier in the shared one) shows up here and not only in the
    # main-thread test.
    _write(tmp_path / "sess" / "subagents" / "agent-abc123def456.jsonl", [
        _asst("s1", _usage(read=50_000, w5=1_000, w1h=1_000, out=2_000),
              [{"type": "text", "text": "работаю"}], sidechain=True),
    ])
    (tmp_path / "sess" / "subagents" / "agent-abc123def456.meta.json").write_text(
        json.dumps({"agentType": "reviewer", "description": "Ревью изменений",
                    "toolUseId": "t1", "spawnDepth": 1}), encoding="utf-8")
    return main


def test_subagent_spend_is_counted_and_tied_to_the_call_that_launched_it(tmp_path):
    """Delegated work is invisible in the parent's usage — the parent only sees
    the final report — so a session that delegates looks cheap until this is
    counted alongside it."""
    session = tokens.parse(_delegating_session(tmp_path))
    tokens.parse_subagents(session)
    assert len(session.subagents) == 1
    agent = session.subagents[0]
    assert agent.agent_id == "abc123def456"
    assert agent.kind.startswith("reviewer"), "the sidecar names what was delegated"
    assert agent.tool_use_id == "t1", "and which call asked for it"

    payload = tokens.summarize(session)
    # abs, not rel: the payload is rounded to four decimals, and the tolerance
    # has to absorb that without absorbing a drifted multiplier — pricing the
    # 5m write at the 1h rate would move this by 0.00375, forty times wider.
    assert payload["subagent_cost_usd"] == pytest.approx(
        ((50_000 * 0.1 + 1_000 * 1.25 + 1_000 * 2.0) * 5.0
         + 2_000 * 25.0) / 1_000_000, abs=1e-4)
    # Not folded into the parent's own bill — money AND tokens. Asserting only
    # the money let a plant that added the subagent's input/output/cache_read
    # into `totals` pass unnoticed, which is exactly the double count the
    # separate section exists to prevent.
    assert payload["totals"]["cost_usd"] < payload["subagent_cost_usd"]
    assert payload["totals"]["cache_read"] == 2_200
    assert payload["totals"]["output"] == 20
    assert payload["totals"]["input"] == 0


def test_a_synchronous_delegation_is_identified_too(tmp_path):
    """A foreground `Agent` call returns the agent's report, not a launch
    envelope, so scraping `agentId:` out of tool output found nothing and half
    the delegations came back unlabelled. The sidecar covers both paths."""
    session = tokens.parse(_delegating_session(tmp_path, synchronous=True))
    tokens.parse_subagents(session)
    assert session.subagents[0].kind.startswith("reviewer")
    assert session.subagents[0].tool_use_id == "t1"


def test_an_agent_id_quoted_in_tool_output_is_not_a_launch(tmp_path):
    """Scanning arbitrary tool output for `agentId:` also matched text that
    merely QUOTED it — a session discussing this module's own fixture
    registered its example id as a real delegation."""
    main = _write(tmp_path / "sess.jsonl", [
        _asst("m1", _usage(read=1_000, out=10), [_tool_use("Bash", "t1", command="cat t.py")]),
        _result("t1", 'x = "Async agent launched successfully. agentId: deadbeefcafe"'),
        _asst("m2", _usage(read=1_200, out=10), [{"type": "text", "text": "ok"}]),
    ])
    session = tokens.parse(main)
    tokens.parse_subagents(session)
    assert session.subagents == []


def test_sidechain_records_in_the_parent_transcript_are_not_double_counted(tmp_path):
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=1_000, out=10), [{"type": "text", "text": "a"}]),
        _asst("side", _usage(read=99_000, out=9_000), [{"type": "text", "text": "b"}],
              sidechain=True),
    ])
    session = tokens.parse(path)
    assert len(session.turns) == 1
    assert session.turns[0].cache_read == 1_000


# --- robustness and the privacy line ----------------------------------------


def test_a_truncated_last_line_is_skipped_not_guessed_at(tmp_path):
    path = tmp_path / "s.jsonl"
    _write(path, [_asst("m1", _usage(read=1_000, out=10), [{"type": "text", "text": "a"}])])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "message": {"id": "m2", "usa')
    session = tokens.parse(path)
    assert len(session.turns) == 1


def test_a_broken_line_in_the_MIDDLE_does_not_end_the_read(tmp_path):
    """With the broken line last, `continue` and `break` are indistinguishable
    — a plant swapping one for the other kept the suite green. A single corrupt
    line in the middle would then silently truncate the rest of the session,
    and every number after it would still look plausible."""
    path = tmp_path / "s.jsonl"
    _write(path, [_asst("m1", _usage(read=1_000, out=10), [{"type": "text", "text": "a"}])])
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"type": "assistant", "message": {"id": "broken", "usa\n')
        fh.write(json.dumps(_asst("m2", _usage(read=2_000, out=10),
                                  [{"type": "text", "text": "b"}])) + "\n")
    session = tokens.parse(path)
    assert [t.message_id for t in session.turns] == ["m1", "m2"], \
        "the turn after the corrupt line was dropped"


def test_reading_a_report_writes_absolutely_nothing(tmp_path, monkeypatch):
    """The property that keeps this outside the session-search opt-in: it
    cannot widen what is stored, because it stores nothing."""
    root = tmp_path / "projects" / "proj"
    _write(root / "sess.jsonl", [
        _asst("m1", _usage(read=1_000, out=10),
              [_tool_use("Read", "t1", file_path="/secret/path.py")]),
        _result("t1", "SECRET_FILE_BODY"),
        _asst("m2", _usage(read=1_500, out=10), [{"type": "text", "text": "ok"}]),
    ])
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")

    def snapshot():
        return {p: p.stat().st_mtime_ns for p in sorted(tmp_path.rglob("*")) if p.is_file()}

    before = snapshot()
    payload = tokens.report()
    assert payload["totals"]["turns"] == 2
    assert snapshot() == before, "a read-only report touched the filesystem"


def test_detail_off_drops_call_targets_for_a_shareable_report(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "proj"
    _write(root / "sess.jsonl", [
        _asst("m1", _usage(read=1_000, out=10),
              [_tool_use("Read", "t1", file_path="/private/client-name.py")]),
        _result("t1", "x" * 2_000),
        _asst("m2", _usage(read=2_000, out=10), [{"type": "text", "text": "ok"}]),
    ])
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")
    plain = json.dumps(tokens.report(detail=False), ensure_ascii=False)
    assert "client-name" not in plain
    assert "client-name" in json.dumps(tokens.report(detail=True), ensure_ascii=False)


def test_a_skill_body_is_charged_to_the_call_that_loaded_it(tmp_path):
    """A loaded skill arrives as a plain user text block — not as the Skill
    call's tool_result and with no marker — so it was filed as something the
    person typed. On a real session that mislabelled 661k characters, 50.3% of
    all context growth, as "user input"."""
    body = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [
        {"type": "text", "text": "Base directory for this skill: /x/y\n" + "s" * 4_000}]}}
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=1_000, out=0), [_tool_use("Skill", "t1", skill="claude-api")]),
        _result("t1", "Launching skill: claude-api"),
        body,
        _asst("m2", _usage(read=2_000, out=0), [{"type": "text", "text": "ok"}]),
    ])
    payload = tokens.summarize(tokens.parse(path))
    by_label = {r["label"]: r["added"] for r in payload["by_source"]}
    assert tokens.USER not in by_label, "the skill body is not something a person typed"
    skill = next(r for r in payload["by_tool"] if r["name"] == "Skill")
    assert skill["added"] > 900, "the body must be charged to the Skill call"


def test_an_unknown_rate_leaves_carry_empty_rather_than_zero(tmp_path):
    """`$0.0000` reads as "free"; the module's own rule is that an unknown
    model produces no number at all."""
    path = _write(tmp_path / "s.jsonl", [
        _asst("m1", _usage(read=1_000, out=0), [_tool_use("Read", "t1", file_path="/a")],
              model="claude-not-released"),
        _result("t1", "x" * 4_000),
        _asst("m2", _usage(read=5_000, out=0), [{"type": "text", "text": "b"}],
              model="claude-not-released"),
        _asst("m3", _usage(read=5_100, out=0), [{"type": "text", "text": "c"}],
              model="claude-not-released"),
    ])
    payload = tokens.summarize(tokens.parse(path))
    assert all(r["carry_usd"] is None for r in payload["by_source"])
    assert all(r["carry_usd"] is None for r in payload["by_tool"])
    assert render.usage(payload).count("$0.0000") == 0


# --- the one-line phase report -----------------------------------------------


def test_brief_counts_only_the_turns_of_the_phase(tmp_path, monkeypatch):
    """The workflow closes each phase with its fuel cost. Producing that by
    dumping `--json` into the context costs ~8k tokens, which made measuring
    the spend the largest item of spend — so this returns the numbers and
    nothing else, and hands the next phase its starting index."""
    root = tmp_path / "projects" / "proj"
    _write(root / "sess.jsonl", [
        _asst("m1", _usage(read=1_000, out=100), [{"type": "text", "text": "a"}]),
        _asst("m2", _usage(read=2_000, out=200), [{"type": "text", "text": "b"}]),
        _asst("m3", _usage(read=3_000, out=300), [{"type": "text", "text": "c"}]),
    ])
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")

    whole = tokens.brief()
    assert whole["turns"] == 3 and whole["output"] == 600
    assert whole["next_turn"] == 3, "the next phase starts where this one ended"

    phase = tokens.brief(start=2)
    assert phase["turns"] == 1 and phase["output"] == 300
    assert phase["tokens"] == 3_300, "only the phase's own turns are counted"


def test_a_delegation_is_billed_to_the_phase_that_made_it(tmp_path, monkeypatch):
    """Charging every sidechain to whichever phase happens to be asking would
    bill a late phase for a delegation made long before it started."""
    root = tmp_path / "projects" / "proj"
    _write(root / "sess.jsonl", [
        _asst("m1", _usage(read=1_000, out=10), [_tool_use("Agent", "t1", subagent_type="x")]),
        _result("t1", "ok"),
        _asst("m2", _usage(read=2_000, out=10), [{"type": "text", "text": "b"}]),
        _asst("m3", _usage(read=3_000, out=10), [{"type": "text", "text": "c"}]),
    ])
    _write(root / "sess" / "subagents" / "agent-aaa111.jsonl", [
        _asst("s1", _usage(read=900_000, out=5_000), [{"type": "text", "text": "…"}],
              sidechain=True),
    ])
    (root / "sess" / "subagents" / "agent-aaa111.meta.json").write_text(
        json.dumps({"agentType": "x", "toolUseId": "t1"}), encoding="utf-8")
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")

    early = tokens.brief(start=0)
    late = tokens.brief(start=2)
    assert early["tokens"] > 900_000, "the phase that delegated pays for it"
    assert late["tokens"] < 10_000, "a later phase must not inherit that bill"


# --- the overview must read the whole window before cutting it ---------------


def _window(tmp_path, monkeypatch, sizes: dict[str, int]):
    """One transcript per entry; `sizes` sets each session's cache reads, and
    files are stamped oldest-last so the priciest is NOT the most recent."""
    root = tmp_path / "projects" / "proj"
    for i, (name, reads) in enumerate(sizes.items()):
        path = _write(root / f"{name}.jsonl", [
            _asst("m1", _usage(read=reads, out=10), [{"type": "text", "text": "a"}]),
            _asst("m2", _usage(read=reads, out=10), [{"type": "text", "text": "b"}]),
        ])
        os.utime(path, (1_000_000 + i, 1_000_000 + i))
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")


def test_the_overview_reads_the_whole_window_before_it_cuts(tmp_path, monkeypatch):
    """The display limit used to stop the SCAN. Sessions are walked
    newest-first, so the most expensive one was never opened unless it happened
    to be recent — and the printed total covered only the rows that survived.
    Measured on real data: `limit=3` claimed $190.67 and named the wrong
    session; the same window read whole is $486.16."""
    _window(tmp_path, monkeypatch, {"expensive": 9_000_000, "mid": 100_000, "recent": 1_000})
    payload = tokens.overview(days=36_500, limit=1)

    assert payload["sessions"][0]["session"] == "expensive", \
        "the cut was applied before the sort"
    assert payload["scanned"] == 3 and payload["shown"] == 1
    whole = tokens.overview(days=36_500, limit=99)
    assert payload["total_usd"] == whole["total_usd"], \
        "the total must cover the window, not the rows that survived the cut"
    assert "of 3" in render.usage_overview(payload), "the report must say it is a subset"


def test_a_session_is_resolved_by_id_prefix(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "proj"
    for name in ("aaaa1111", "bbbb2222"):
        _write(root / f"{name}.jsonl",
               [_asst("m1", _usage(read=1_000, out=10), [{"type": "text", "text": "a"}])])
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")
    assert tokens.resolve("bbbb").stem == "bbbb2222"
    assert tokens.resolve("nope") is None


def test_an_empty_transcript_answers_instead_of_crashing(tmp_path, monkeypatch):
    root = tmp_path / "projects" / "proj"
    _write(root / "sess.jsonl", [{"type": "mode", "mode": "default"}])
    monkeypatch.setattr(tokens, "claude_transcript_root", lambda: tmp_path / "projects")
    assert "error" in tokens.report()
