"""What each step of a session cost, and what it left behind in the context.

Two different questions hide inside "how many tokens did this take", and they
have different answers:

* **What this turn was billed** — the `usage` block, priced per category. Good
  for "which turns were expensive", useless for finding leaks: on a long
  session nearly every input token is a cache read of the same prefix, so the
  number climbs monotonically no matter what the turn actually did.

* **What this step ADDED to the prefix and left there** — the difference
  between one turn's prompt size and the next one's. This is the leak metric.
  A 40k `Read` is not a 40k event: it is 40k re-read on every later turn for
  the rest of the session, which is what `carry` below prices.

The second is derived from the first. `input + cache_read + cache_creation` is
the exact size of the prompt the model saw, so the delta between consecutive
turns is the exact number of tokens the step in between added. No estimation
is involved in that total — only its ATTRIBUTION to individual tool calls is
estimated, by splitting the delta across the blocks that appeared, in
proportion to their measured size (`_attribute`). The per-step total is
therefore trustworthy; a single tool's share of it is an estimate, and is
labelled as one everywhere it is reported.

THE TRAP this module exists to avoid: Claude Code writes one JSONL record per
content block (thinking, text, each `tool_use`) and repeats the SAME `usage`
in every one of them. Measured on this machine: 163 assistant records for 65
messages, with `usage` byte-identical across all 49 multi-record messages.
Summing records instead of messages overstates the bill by ~1.8x — and the
result looks entirely plausible. Everything here groups by `message.id` first.
The second half of the same trap: the `tool_use` block is usually NOT in a
message's first record, so a reader that keeps only the first record loses the
tool calls. Content blocks are merged across every record of a message.

Privacy: this module reads transcripts and returns numbers. It never writes
anything — no cache, no counters, no state — so it cannot widen what `.mb/`
holds. It stays under the `mb reflect` guarantee by not persisting at all,
which is also why a truncated call `target` ("which Read was the 40k one") is
safe here: it lives in one printed report and is never stored. Callers that
share the output can drop it with `detail=False`. See
[[TRANSCRIPTS D1 - приватность транскриптов]].
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .reflect import claude_transcript_root

# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------
# USD per million tokens. Only models whose published rates are known are
# listed: an unknown model yields NO cost rather than a guessed one, and the
# report says so. Token counts stay correct either way — it is the dollar
# column that goes silent, which is the honest failure mode.
#
# Not modelled, deliberately: promotional pricing (Sonnet 5's introductory
# rate), Batch API's 50% discount, and any partner-platform rate card
# (Bedrock/Vertex are billed by the partner). All three would need a fact this
# file cannot read out of a transcript.


@dataclass(frozen=True)
class Rate:
    """Per-million-token prices for one model at one speed."""

    input: float
    output: float


STANDARD: dict[str, Rate] = {
    "claude-fable-5": Rate(10.0, 50.0),
    "claude-mythos-5": Rate(10.0, 50.0),
    "claude-opus-5": Rate(5.0, 25.0),
    "claude-opus-4-8": Rate(5.0, 25.0),
    "claude-opus-4-7": Rate(5.0, 25.0),
    "claude-opus-4-6": Rate(5.0, 25.0),
    "claude-sonnet-5": Rate(3.0, 15.0),
    "claude-sonnet-4-6": Rate(3.0, 15.0),
    "claude-haiku-4-5": Rate(1.0, 5.0),
}

# `speed: "fast"` is a premium tier, not a hint — read the field, never assume.
# Opus 5's $10/$50 is published. Fast mode is documented as available on Opus
# 4.8 as well, but its price is not stated separately anywhere I could check —
# the figure below is the Opus 5 rate carried over, and it is written down here
# as an extrapolation rather than left to look like a quoted fact.
FAST: dict[str, Rate] = {
    "claude-opus-5": Rate(10.0, 50.0),
    "claude-opus-4-8": Rate(10.0, 50.0),      # extrapolated, see above
}

# Input categories are NOT interchangeable, so a single "input tokens" number
# is meaningless. Multipliers against the model's input rate:
CACHE_READ = 0.1
WRITE_5M = 1.25
WRITE_1H = 2.0

_SUFFIX = re.compile(r"\[[^\]]*\]$")          # claude-opus-5[1m]
_DATED = re.compile(r"-\d{8}$")               # claude-haiku-4-5-20251001
_PLATFORM = re.compile(r"^(anthropic|us|eu|apac|global)\.")   # bedrock/vertex


def normalize_model(model: str) -> str:
    """`claude-opus-5[1m]`, `anthropic.claude-opus-5`, `claude-haiku-4-5-20251001`
    all price as the same model. The context-window suffix is a deployment
    variant, not a rate card."""
    m = (model or "").strip().lower()
    m = _PLATFORM.sub("", m)
    m = _SUFFIX.sub("", m)
    return _DATED.sub("", m)


def rate_for(model: str, speed: str | None) -> Rate | None:
    """None means "this model's price is not known here" — the caller reports
    tokens and leaves the money column empty rather than inventing a rate."""
    key = normalize_model(model)
    if speed == "fast" and key in FAST:
        return FAST[key]
    return STANDARD.get(key)


# ---------------------------------------------------------------------------
# the shapes a report is built from
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One `tool_use` and what came back for it."""

    name: str
    tool_use_id: str
    target: str = ""            # truncated argument, never persisted
    result_chars: int = 0       # measured size of the tool_result
    is_error: bool = False
    added: int = 0              # ESTIMATED share of the step's context growth
    carry_tokens: int = 0       # `added` re-read on every later turn
    carry_usd: float | None = None   # None = no rate on file, never 0.0


@dataclass
class Turn:
    """One assistant message — the unit that is billed exactly once."""

    index: int
    message_id: str
    timestamp: str = ""
    model: str = ""
    speed: str | None = None
    input_tokens: int = 0
    cache_read: int = 0
    write_5m: int = 0
    write_1h: int = 0
    write_other: int = 0        # legacy transcripts: total with no TTL split
    output_tokens: int = 0
    prefix: int = 0             # exact prompt size the model saw
    delta: int | None = None    # prefix growth caused by this step
    reset: bool = False         # the prefix shrank: compaction / context clear
    cost_usd: float | None = None
    tools: list[ToolCall] = field(default_factory=list)
    text_chars: int = 0
    thinking_chars: int = 0

    @property
    def billed_input(self) -> int:
        """Every input category summed — for "how big was this prompt", never
        for pricing (each category has its own multiplier)."""
        return (self.input_tokens + self.cache_read
                + self.write_5m + self.write_1h + self.write_other)


@dataclass
class Source:
    """One thing that grew the context, aggregated over the session.

    Every token that entered the prefix came from somewhere, and a report that
    only lists tool calls hides most of it: on a long coding session the
    assistant's own output is usually the largest single source, and hook /
    system-reminder injections are the one that surprises people. The sources
    below therefore have to ADD UP to the measured growth — an attribution
    that silently drops two thirds of the tokens reads as "tools are cheap"
    when the truth is "we never looked at the rest".
    """

    label: str
    added: int = 0
    events: int = 0
    carry_tokens: int = 0
    carry_usd: float | None = None


@dataclass
class Subagent:
    """A sidechain transcript: `<session>/subagents/agent-<id>.jsonl`.

    Its spend is invisible in the parent's `usage` — the parent only ever sees
    the agent's final report — so a session that delegates looks cheap until
    these are counted alongside it."""

    agent_id: str
    path: Path
    turns: list[Turn] = field(default_factory=list)
    kind: str = ""              # subagent_type from the launching Agent call
    tool_use_id: str = ""


@dataclass
class Session:
    """One top-level transcript, plus every sidechain it launched."""

    session_id: str
    path: Path
    turns: list[Turn] = field(default_factory=list)
    subagents: list[Subagent] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unpriced: set[str] = field(default_factory=set)
    injected_chars: int = 0     # hook/system/attachment text between turns
    growth: dict[str, Source] = field(default_factory=dict)
    reclaimed: int = 0          # tokens a compaction/clear gave back
    mtime: float = 0.0


# ---------------------------------------------------------------------------
# reading a transcript
# ---------------------------------------------------------------------------

# Growth sources that are not a tool call. Named constants because they are
# also the keys a `--json` consumer reads.
ASSISTANT = "assistant output (text+thinking)"
INJECTED = "context injection (hooks, reminders, attachments)"
USER = "user input"
UNMATCHED = "tool result (call not found)"
UNATTRIBUTED = "unattributed"

# Text that arrives in a `user` message but was not typed by the user: hook
# output, system reminders, task notifications, slash-command envelopes. It is
# reported as an injection, because that is the question being asked — "which
# machinery is filling my context" — and lumping it into "user input" hides
# the one source the owner can actually turn off.
_INJECTED_MARKERS = ("<system-reminder>", "<task-notification>",
                     "<local-command-", "<command-name>", "<command-message>",
                     "<command-args>", "<user-prompt-submit-hook>",
                     "<session-start-hook>", "<post-tool-use-hook>")

# A loaded skill's body arrives as a plain `text` block in a user message — not
# as the `tool_result` of the `Skill` call that asked for it, and with no
# marker of its own. Left unlabelled it is filed as something the person typed,
# which is how the largest single source in a real session (661,442 characters
# = 50.3% of that session's context growth) came to be reported as "user
# input". The body is charged back to the `Skill` call in the same step.
_SKILL_BODY = "Base directory for this skill:"
SKILL_BODY = "skill body (loaded into context)"


def _user_label(text: str) -> str:
    head = text[:400]
    if _SKILL_BODY in head:
        return SKILL_BODY
    return INJECTED if any(m in head for m in _INJECTED_MARKERS) else USER
# The fields worth showing for "which call was the heavy one", most specific
# first. `prompt`/`description` come last: they are the longest and the least
# identifying of the lot.
_TARGET_KEYS = ("file_path", "path", "pattern", "command", "query", "url",
                "notebook_path", "skill", "subagent_type", "description",
                "prompt")


def _records(path: Path) -> Iterator[dict[str, Any]]:
    """Malformed lines are skipped, never guessed at. A truncated last line is
    normal — transcripts are appended to while they are being read."""
    try:
        handle = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _usage_totals(usage: dict[str, Any]) -> tuple[dict[str, int], int]:
    """Billable token counts, and the size of the prompt that was actually sent.

    These are two different numbers whenever a request took more than one
    attempt (a refusal fallback, a retry): every attempt is billed, but only
    the last one's prompt size is the prefix the NEXT turn grows from. Summing
    `iterations` for the prefix would double-count the whole conversation.
    """
    iterations = usage.get("iterations")
    attempts: list[dict[str, Any]] = []
    if isinstance(iterations, list):
        attempts = [a for a in iterations if isinstance(a, dict)]

    def one(block: dict[str, Any]) -> dict[str, int]:
        creation = block.get("cache_creation")
        creation = creation if isinstance(creation, dict) else {}
        m5 = int(creation.get("ephemeral_5m_input_tokens") or 0)
        h1 = int(creation.get("ephemeral_1h_input_tokens") or 0)
        total_write = int(block.get("cache_creation_input_tokens") or 0)
        # Old transcripts carry only the total, with no TTL split. Price the
        # remainder at the CHEAP rate rather than inventing the expensive one.
        other = max(0, total_write - m5 - h1)
        return {
            "input": int(block.get("input_tokens") or 0),
            "cache_read": int(block.get("cache_read_input_tokens") or 0),
            "write_5m": m5,
            "write_1h": h1,
            "write_other": other,
            "output": int(block.get("output_tokens") or 0),
        }

    if len(attempts) > 1:
        billed = dict.fromkeys(one({}), 0)
        for a in attempts:
            for k, v in one(a).items():
                billed[k] += v
        last = one(attempts[-1])
    else:
        billed = one(usage)
        last = billed
    prefix = (last["input"] + last["cache_read"]
              + last["write_5m"] + last["write_1h"] + last["write_other"])
    return billed, prefix


def _target(block: dict[str, Any]) -> str:
    """A short, human-facing descriptor of what a call was aimed at."""
    payload = block.get("input")
    if not isinstance(payload, dict):
        return ""
    for key in _TARGET_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            flat = " ".join(value.split())
            return flat[:70] + "…" if len(flat) > 70 else flat
    return ""


def _content_chars(content: Any) -> int:
    """Size of a tool_result payload, whichever shape it arrived in."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if isinstance(block, dict):
                total += len(block.get("text") or "")
                if block.get("type") == "image":
                    # An image is not free just because it has no text. This is
                    # a placeholder weight, not a token count — it only has to
                    # keep images from being attributed a share of zero.
                    total += 4000
            elif isinstance(block, str):
                total += len(block)
        return total
    return 0


def parse(path: Path, *, detail: bool = True) -> Session:
    """One transcript into turns, in the order the work happened.

    Sidechain records are skipped here (`isSidechain`), not because they are
    cheap but because they are a different accounting unit — they are read
    separately by `parse_subagents` and reported alongside.
    """
    session = Session(session_id=path.stem, path=path)
    try:
        session.mtime = path.stat().st_mtime
    except OSError:
        session.mtime = 0.0

    order: list[str] = []
    by_id: dict[str, Turn] = {}
    blocks: dict[str, list[dict[str, Any]]] = {}
    usage_seen: dict[str, str] = {}
    # tool_use_id -> the turn that issued it, so a result can be charged back
    # to the call that caused it even when it lands several records later.
    issued: dict[str, ToolCall] = {}
    # What arrived AFTER turn i and before turn i+1, with its measured size.
    # This is what a step's context growth gets attributed across.
    arrivals: dict[int, list[tuple[str, int, str, bool]]] = {}
    multi_record = 0
    conflicting = 0
    synthetic_ids: set[str] = set()

    def current_slot() -> int:
        return len(order) - 1

    for record in _records(path):
        kind = record.get("type")
        if record.get("isSidechain"):
            continue

        if kind == "assistant":
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            mid = message.get("id") or record.get("uuid") or ""
            usage = message.get("usage")
            usage = usage if isinstance(usage, dict) else {}

            if mid in synthetic_ids:
                continue

            if mid not in by_id:
                billed, prefix = _usage_totals(usage)
                # A client-side artefact, not a turn: `model: "<synthetic>"`
                # rows such as "API Error: Connection closed mid-response" carry
                # an all-zero usage. Counting one as a turn puts a prompt size
                # of 0 between two real turns, which reads as a context reset
                # AND an equally large regrowth on the next step — measured on
                # a real session, that single row invented 580k of growth and
                # charged it to whatever happened to arrive next.
                if prefix == 0 and billed["output"] == 0:
                    synthetic_ids.add(mid)
                    continue
                turn = Turn(
                    index=len(order),
                    message_id=mid,
                    timestamp=str(record.get("timestamp") or ""),
                    model=str(message.get("model") or ""),
                    speed=usage.get("speed"),
                    input_tokens=billed["input"],
                    cache_read=billed["cache_read"],
                    write_5m=billed["write_5m"],
                    write_1h=billed["write_1h"],
                    write_other=billed["write_other"],
                    output_tokens=billed["output"],
                    prefix=prefix,
                )
                by_id[mid] = turn
                order.append(mid)
                blocks[mid] = []
                usage_seen[mid] = json.dumps(usage, sort_keys=True)
            else:
                multi_record += 1
                # The repeat is expected and must NOT be added. A repeat that
                # disagrees is not expected — say so rather than pick one.
                if usage and json.dumps(usage, sort_keys=True) != usage_seen[mid]:
                    conflicting += 1

            content = message.get("content")
            if isinstance(content, list):
                blocks[mid].extend(b for b in content if isinstance(b, dict))
            continue

        if kind == "user":
            message = record.get("message")
            content = (message or {}).get("content") if isinstance(message, dict) else None
            slot = current_slot()
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        if isinstance(block, dict):
                            text = block.get("text") or ""
                            if text and slot >= 0:
                                arrivals.setdefault(slot, []).append(
                                    (_user_label(text), len(text), "", False))
                        continue
                    # The label stays provisional here: the `tool_use` this
                    # result answers is usually in a LATER record of an
                    # EARLIER message (the duplication trap), so the call it
                    # belongs to is not known until every message's blocks
                    # have been merged. Resolved in one pass below.
                    if slot >= 0:
                        arrivals.setdefault(slot, []).append((
                            UNMATCHED,
                            _content_chars(block.get("content")),
                            str(block.get("tool_use_id") or ""),
                            bool(block.get("is_error")),
                        ))
            elif isinstance(content, str) and content and slot >= 0:
                arrivals.setdefault(slot, []).append(
                    (_user_label(content), len(content), "", False))
            continue

        if kind in ("attachment", "system"):
            # Hook output, system reminders, queued commands, file attachments:
            # they cost nothing to produce and stay in the prefix forever.
            slot = current_slot()
            if slot < 0:
                continue
            size = len(json.dumps(record.get("attachment") or record.get("content") or "",
                                  ensure_ascii=False))
            session.injected_chars += size
            arrivals.setdefault(slot, []).append((INJECTED, size, "", False))
            continue

    # Content blocks are merged across every record of a message before tool
    # calls are read out of them -- the second half of the duplication trap.
    for mid in order:
        turn = by_id[mid]
        for block in blocks[mid]:
            btype = block.get("type")
            if btype == "tool_use":
                call = ToolCall(
                    name=str(block.get("name") or "?"),
                    tool_use_id=str(block.get("id") or ""),
                    target=_target(block) if detail else "",
                )
                turn.tools.append(call)
                if call.tool_use_id:
                    issued[call.tool_use_id] = call
            elif btype == "text":
                turn.text_chars += len(block.get("text") or "")
            elif btype == "thinking":
                turn.thinking_chars += len(block.get("thinking") or "")

    # Now that every message's blocks are merged, each tool_result can be tied
    # back to the call it answers — the size of what came back belongs to the
    # call, not to an anonymous "tool result" bucket.
    orphans = 0
    for slot, items in arrivals.items():
        resolved: list[tuple[str, int, str, bool]] = []
        for label, size, tid, err in items:
            call = issued.get(tid) if tid else None
            if call is not None:
                call.result_chars += size
                call.is_error = call.is_error or err
                resolved.append((call.name, size, tid, err))
            else:
                orphans += int(label == UNMATCHED)
                resolved.append((label, size, tid, err))
        arrivals[slot] = resolved

    session.turns = [by_id[mid] for mid in order]
    _price(session)
    _deltas(session)
    _attribute(session, arrivals)
    if orphans:
        session.warnings.append(
            f"{orphans} tool result(s) had no matching call in this transcript "
            "(a resumed or forked session starts mid-conversation)")

    if synthetic_ids:
        session.warnings.append(
            f"{len(synthetic_ids)} client-side row(s) with no usage (API errors, "
            "synthetic messages) were not counted as turns")
    if conflicting:
        session.warnings.append(
            f"{conflicting} message(s) repeated a DIFFERENT usage block across their "
            "records; the first was used")
    if multi_record:
        session.warnings.append(
            f"{len(session.turns)} messages across {len(session.turns) + multi_record} "
            "records — usage counted once per message, not per record")
    return session


# ---------------------------------------------------------------------------
# derived numbers
# ---------------------------------------------------------------------------


def cost_of(turn: Turn, rate: Rate | None) -> float | None:
    """The one place a turn is priced.

    It used to be two: the main thread and the sidechain reader each carried
    their own copy of the same expression. Planting `WRITE_5M`->`WRITE_1H` in
    the first turned the suite red and in the second changed nothing at all, so
    any future edit to the rate table could have silently moved only the
    subagents' money.
    """
    if rate is None:
        return None
    return (
        (turn.input_tokens
         + turn.cache_read * CACHE_READ
         + turn.write_5m * WRITE_5M
         + turn.write_1h * WRITE_1H
         + turn.write_other * WRITE_5M) * rate.input
        + turn.output_tokens * rate.output
    ) / 1_000_000


def _price(session: Session) -> None:
    for turn in session.turns:
        rate = rate_for(turn.model, turn.speed)
        if rate is None:
            if turn.model:
                session.unpriced.add(turn.model)
            continue
        turn.cost_usd = cost_of(turn, rate)


def _deltas(session: Session) -> None:
    """How much each step added to the prefix the next turn had to carry.

    A NEGATIVE delta is not an error: compaction and context-clearing both
    shrink the prefix. It is recorded as a reset instead of being clamped,
    because "the context was rebuilt here" is exactly what a reader chasing a
    leak needs to see.
    """
    for i, turn in enumerate(session.turns[:-1]):
        turn.delta = session.turns[i + 1].prefix - turn.prefix
        turn.reset = turn.delta < 0


def _attribute(session: Session,
               arrivals: dict[int, list[tuple[str, int, str, bool]]]) -> None:
    """Split each step's measured growth across what arrived during it, and
    price what that growth costs for the REST of the session.

    Three properties, in the order they matter:

    * **The step total is exact** — it is the difference between two reported
      prompt sizes, not an estimate.
    * **The split adds up.** Every token of every positive delta lands in some
      source, `unattributed` included. A breakdown that quietly covers a third
      of the growth would make tool calls look cheap by omission.
    * **The split itself is approximate.** Shares are proportional to measured
      character counts, and a JSON tool result does not tokenize at the same
      rate as prose. Good enough to rank sources — which is the question — and
      never presented as better than that. The assistant's own output is the
      exception: `output_tokens` is exact, so it is taken out first.

    `carry` is the number the leak question actually wants. Tokens added at
    turn i are re-read as cache on every later turn until the prefix is reset,
    so a 40k read early in a 400-turn session is not a 40k event — it is 40k
    re-read 400 times. That cost appears nowhere in the bill of the turn that
    caused it.
    """
    last = len(session.turns) - 1
    resets = [t.index for t in session.turns if t.reset]

    for turn in session.turns:
        if turn.delta is None:
            continue
        if turn.delta < 0:
            session.reclaimed += -turn.delta
            continue
        # Carrying stops at the next context reset, not at the end of the run.
        stop = next((r for r in resets if r >= turn.index), last)
        remaining = max(0, stop - turn.index)
        rate = rate_for(turn.model, turn.speed)

        # What the model wrote is known exactly and all of it lands in the
        # next prompt — unless the prefix was trimmed, hence the min().
        own = min(turn.output_tokens, turn.delta)
        _credit(session, ASSISTANT, own, remaining, rate)

        remainder = turn.delta - own
        if remainder <= 0:
            continue
        items = arrivals.get(turn.index) or []
        total_chars = sum(size for _, size, _, _ in items)
        if total_chars <= 0:
            _credit(session, UNATTRIBUTED, remainder, remaining, rate)
            continue
        by_call = {c.tool_use_id: c for c in turn.tools if c.tool_use_id}
        skill_call = next((c for c in turn.tools if c.name == "Skill"), None)
        handed_out = 0
        for label, size, tid, _err in items:
            share = int(remainder * size / total_chars)
            handed_out += share
            call = by_call.get(tid)
            if call is None and label == SKILL_BODY and skill_call is not None:
                # The body carries no tool_use_id of its own; the only link to
                # the call that loaded it is being in the same step.
                call, label = skill_call, skill_call.name
            _credit(session, label, share, remaining, rate, call)
        # Integer division loses a few tokens per step; they are real tokens
        # and are reported rather than dropped.
        _credit(session, UNATTRIBUTED, remainder - handed_out, remaining, rate)


def _credit(session: Session, label: str, tokens: int, remaining: int,
            rate: Rate | None, call: ToolCall | None = None) -> None:
    """Book `tokens` to one source, plus the cost of re-reading them on the
    `remaining` turns that still carry them.

    With no rate on file the money stays None rather than becoming 0.0: this
    module already refuses to invent a price for an unknown model, and a
    printed `$0.0000` reads as "free" — the opposite of "unknown" — in the very
    column the metric exists for.
    """
    if tokens <= 0:
        return
    carried = tokens * remaining
    usd = carried * rate.input * CACHE_READ / 1_000_000 if rate is not None else None
    src = session.growth.setdefault(label, Source(label))
    src.added += tokens
    src.events += 1
    src.carry_tokens += carried
    if usd is not None:
        src.carry_usd = (src.carry_usd or 0.0) + usd
    if call is not None:
        call.added += tokens
        call.carry_tokens += carried
        if usd is not None:
            call.carry_usd = (call.carry_usd or 0.0) + usd


# ---------------------------------------------------------------------------
# subagents
# ---------------------------------------------------------------------------


def subagent_dir(path: Path) -> Path:
    return path.with_suffix("") / "subagents"


def parse_subagents(session: Session, *, detail: bool = True) -> None:
    """Sidechain transcripts, tied back to the call that launched them.

    The link comes from `agent-<id>.meta.json`, written next to every sidechain
    transcript: `agentType` is what was delegated, `toolUseId` is the `Agent`
    call that asked for it.

    The first version scraped `agentId:` out of the launch tool_result instead,
    and that only ever worked for BACKGROUND launches — those return a metadata
    envelope naming the id, while a synchronous `Agent` call returns the
    agent's report and nothing else, so half the delegations silently had no
    kind at all. Worse, scanning arbitrary tool output for the pattern picks up
    any text that merely quotes it: a session that discussed this module's own
    test fixture registered its example id as a real launch. The sidecar is
    written by the client for exactly this purpose and needs no guessing.
    """
    root = subagent_dir(session.path)
    if not root.is_dir():
        return

    for file in sorted(root.glob("*.jsonl")):
        agent_id = file.stem.replace("agent-", "")
        turns = _sidechain_turns(file)
        if not turns:
            continue
        meta = _sidecar(file)
        kind = str(meta.get("agentType") or "")
        # `description` is free text the user wrote; `agentType` is a name from
        # the agent registry, so only the former is held back by `detail=False`.
        if detail and meta.get("description"):
            kind = f"{kind} — {meta['description']}" if kind else str(meta["description"])
        agent = Subagent(agent_id=agent_id, path=file, turns=turns,
                         kind=kind if detail else str(meta.get("agentType") or ""),
                         tool_use_id=str(meta.get("toolUseId") or ""))
        for turn in turns:
            if turn.model and rate_for(turn.model, turn.speed) is None:
                session.unpriced.add(turn.model)
        session.subagents.append(agent)


def _sidecar(transcript: Path) -> dict[str, Any]:
    """`agent-<id>.meta.json` next to a sidechain transcript, or {} if absent —
    an older client may not have written one, and a missing kind is reported as
    missing rather than guessed at."""
    path = transcript.with_suffix(".meta.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sidechain_turns(path: Path) -> list[Turn]:
    """A subagent's own turns. Same duplication trap, same grouping."""
    order: list[str] = []
    by_id: dict[str, Turn] = {}
    for record in _records(path):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            continue
        mid = message.get("id") or record.get("uuid") or ""
        if mid in by_id:
            continue
        usage = message.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        billed, prefix = _usage_totals(usage)
        turn = Turn(
            index=len(order), message_id=mid,
            timestamp=str(record.get("timestamp") or ""),
            model=str(message.get("model") or ""), speed=usage.get("speed"),
            input_tokens=billed["input"], cache_read=billed["cache_read"],
            write_5m=billed["write_5m"], write_1h=billed["write_1h"],
            write_other=billed["write_other"], output_tokens=billed["output"],
            prefix=prefix,
        )
        turn.cost_usd = cost_of(turn, rate_for(turn.model, turn.speed))
        by_id[mid] = turn
        order.append(mid)
    return [by_id[m] for m in order]


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def sessions_for(project: str | None = None) -> list[Path]:
    """Top-level transcripts, newest first.

    Never descends into `<session-id>/subagents/` — those are read through the
    session that launched them, so that delegated spend is reported as part of
    the session that caused it instead of as a peer of it.
    """
    root = claude_transcript_root()
    if not root.is_dir():
        return []
    found: list[tuple[float, Path]] = []
    for project_dir in sorted(root.iterdir()):
        if not project_dir.is_dir():
            continue
        if project and project.lower() not in project_dir.name.lower():
            continue
        for path in project_dir.glob("*.jsonl"):
            try:
                found.append((path.stat().st_mtime, path))
            except OSError:
                continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    return [path for _, path in found]


def resolve(session: str | None = None, project: str | None = None) -> Path | None:
    """A session id (or unique prefix of one), else the most recent transcript."""
    candidates = sessions_for(project)
    if not candidates:
        return None
    if not session:
        return candidates[0]
    exact = [p for p in candidates if p.stem == session]
    if exact:
        return exact[0]
    partial = [p for p in candidates if p.stem.startswith(session)]
    return partial[0] if partial else None


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def _turn_row(turn: Turn) -> dict[str, Any]:
    return {
        "index": turn.index,
        "at": turn.timestamp,
        "model": turn.model,
        "speed": turn.speed,
        "input": turn.input_tokens,
        "cache_read": turn.cache_read,
        "cache_write": turn.write_5m + turn.write_1h + turn.write_other,
        "output": turn.output_tokens,
        "prompt_size": turn.prefix,
        "added_to_context": turn.delta,
        "context_reset": turn.reset,
        "cost_usd": round(turn.cost_usd, 6) if turn.cost_usd is not None else None,
        "tools": [t.name for t in turn.tools],
    }


def summarize(session: Session, *, top: int = 12) -> dict[str, Any]:
    """Everything the report needs, already aggregated. No formatting here —
    the domain does not print ([[test_architecture]]), it answers."""
    turns = session.turns
    totals = {
        "turns": len(turns),
        "input": sum(t.input_tokens for t in turns),
        "cache_read": sum(t.cache_read for t in turns),
        "cache_write": sum(t.write_5m + t.write_1h + t.write_other for t in turns),
        "output": sum(t.output_tokens for t in turns),
    }
    priced = [t.cost_usd for t in turns if t.cost_usd is not None]
    totals["cost_usd"] = round(sum(priced), 4) if priced else None
    totals["tool_calls"] = sum(len(t.tools) for t in turns)

    # by tool: the "where is the heavy functionality" cut
    agg: dict[str, dict[str, Any]] = {}
    for turn in turns:
        for call in turn.tools:
            row = agg.setdefault(call.name, {
                "name": call.name, "calls": 0, "errors": 0, "added": 0,
                "carry_tokens": 0, "carry_usd": None, "result_chars": 0,
                "heaviest": None, "heaviest_added": 0,
            })
            row["calls"] += 1
            row["errors"] += int(call.is_error)
            row["added"] += call.added
            row["carry_tokens"] += call.carry_tokens
            if call.carry_usd is not None:
                row["carry_usd"] = (row["carry_usd"] or 0.0) + call.carry_usd
            row["result_chars"] += call.result_chars
            if call.added >= row["heaviest_added"]:
                row["heaviest_added"] = call.added
                row["heaviest"] = call.target or None
    by_tool = sorted(agg.values(), key=lambda r: r["added"], reverse=True)
    for row in by_tool:
        if row["carry_usd"] is not None:
            row["carry_usd"] = round(row["carry_usd"], 4)

    # Where the context came from. These add up to the measured growth by
    # construction — `unattributed` is a row, not a rounding hole.
    grew = sum(t.delta for t in turns if t.delta and t.delta > 0)
    sources = sorted(session.growth.values(), key=lambda s: s.added, reverse=True)
    by_source = [{
        "label": s.label,
        "added": s.added,
        "share": round(s.added / grew, 4) if grew else None,
        "events": s.events,
        "carry_tokens": s.carry_tokens,
        "carry_usd": round(s.carry_usd, 4) if s.carry_usd is not None else None,
    } for s in sources]

    growth = [t for t in turns if t.delta is not None and t.delta > 0]
    heaviest_steps = sorted(growth, key=lambda t: t.delta or 0, reverse=True)[:top]
    costly_steps = sorted([t for t in turns if t.cost_usd is not None],
                          key=lambda t: t.cost_usd or 0.0, reverse=True)[:top]

    start = turns[0].prefix if turns else 0
    peak = max((t.prefix for t in turns), default=0)
    end = turns[-1].prefix if turns else 0
    # What the starting prefix alone cost to carry: system prompt, tool
    # schemas, CLAUDE.md and memory are re-read on every single turn, and no
    # amount of tidying the conversation touches them.
    baseline_rate = rate_for(turns[0].model, turns[0].speed) if turns else None
    baseline_carry = (start * max(0, len(turns) - 1) * baseline_rate.input * CACHE_READ
                      / 1_000_000) if baseline_rate else None

    subs = []
    for agent in session.subagents:
        sub_priced = [t.cost_usd for t in agent.turns if t.cost_usd is not None]
        subs.append({
            "agent_id": agent.agent_id,
            "kind": agent.kind,
            "turns": len(agent.turns),
            "output": sum(t.output_tokens for t in agent.turns),
            "cache_read": sum(t.cache_read for t in agent.turns),
            "cache_write": sum(t.write_5m + t.write_1h + t.write_other
                               for t in agent.turns),
            "input": sum(t.input_tokens for t in agent.turns),
            "peak_prompt": max((t.prefix for t in agent.turns), default=0),
            "cost_usd": round(sum(sub_priced), 4) if sub_priced else None,
        })
    subs.sort(key=lambda r: r["cost_usd"] or 0.0, reverse=True)
    sub_cost = [r["cost_usd"] for r in subs if r["cost_usd"] is not None]

    warnings = list(session.warnings)
    if session.unpriced:
        warnings.append("no published rate on file for: "
                        + ", ".join(sorted(session.unpriced))
                        + " — token counts are exact, cost is omitted")

    return {
        "session": session.session_id,
        "path": str(session.path),
        "models": sorted({t.model for t in turns if t.model}),
        "totals": totals,
        "context": {
            "start": start,
            "peak": peak,
            "end": end,
            "grew": grew,
            "reclaimed": session.reclaimed,
            "resets": sum(1 for t in turns if t.reset),
            "injected_chars": session.injected_chars,
            "baseline_carry_usd": (round(baseline_carry, 4)
                                   if baseline_carry is not None else None),
        },
        "by_source": by_source,
        "by_tool": by_tool[:top],
        "heaviest_steps": [_turn_row(t) for t in heaviest_steps],
        "costly_steps": [_turn_row(t) for t in costly_steps],
        "turns": [_turn_row(t) for t in turns],
        "subagents": subs,
        "subagent_cost_usd": round(sum(sub_cost), 4) if sub_cost else None,
        "warnings": warnings,
    }


def brief(session_id: str | None = None, *, project: str | None = None,
          start: int = 0) -> dict[str, Any]:
    """One line's worth of numbers for a range of turns — the phase report.

    The workflow closes each phase with its fuel cost, and the only honest way
    to produce that used to be dumping `--json` into the context and adding it
    up. That dump is ~8k tokens, so measuring the spend became the largest
    single item of spend. This returns the four numbers the line needs and
    nothing else; `next_turn` is what the following phase passes back as
    `start`, so no caller has to do arithmetic or keep a scratch file.
    """
    path = resolve(session_id, project)
    if path is None:
        return {"error": "no transcript found"}
    session = parse(path, detail=False)
    parse_subagents(session, detail=False)
    if not session.turns:
        return {"error": "transcript has no assistant turns", "path": str(path)}

    turns = [t for t in session.turns if t.index >= start]
    # Subagent spend belongs to the phase that DELEGATED, and the parent's own
    # usage never shows it — a phase that fans out looks free without this. It
    # is charged by the turn that launched it, not to whichever phase happens
    # to be asking: counting every sidechain in the session would bill a late
    # phase for a delegation made long before it started.
    launched_at = {c.tool_use_id: t.index
                   for t in session.turns for c in t.tools if c.tool_use_id}
    sub_turns = [
        t for a in session.subagents
        # An older client wrote no sidecar, so the launch is unknown; such an
        # agent counts only in a whole-session report, where it cannot be
        # attributed to the wrong phase.
        if launched_at.get(a.tool_use_id, 0 if start == 0 else -1) >= start
        for t in a.turns
    ]
    priced = [t.cost_usd for t in turns + sub_turns if t.cost_usd is not None]
    return {
        "session": session.session_id,
        "from_turn": start,
        "next_turn": session.turns[-1].index + 1,
        "turns": len(turns),
        "tokens": sum(t.billed_input + t.output_tokens for t in turns + sub_turns),
        "output": sum(t.output_tokens for t in turns),
        "cost_usd": round(sum(priced), 4) if priced else None,
    }


def report(session_id: str | None = None, *, project: str | None = None,
           top: int = 12, detail: bool = True,
           with_subagents: bool = True) -> dict[str, Any]:
    """Resolve a session, read it, and answer. Nothing is written."""
    path = resolve(session_id, project)
    if path is None:
        return {"error": "no transcript found",
                "root": str(claude_transcript_root())}
    session = parse(path, detail=detail)
    if with_subagents:
        parse_subagents(session, detail=detail)
    if not session.turns:
        return {"error": "transcript has no assistant turns", "path": str(path)}
    return summarize(session, top=top)


def overview(days: int = 7, *, project: str | None = None,
             limit: int = 20) -> dict[str, Any]:
    """Cost per session across a window — which sessions to look inside.

    The whole window is read before anything is cut. An earlier version stopped
    scanning once `limit` rows were collected and sorted by cost afterwards,
    which made the display limit silently cut the SCAN: sessions are walked
    newest-first, so the most expensive one was never opened if it was not
    among the most recent. Measured on this machine — `limit=3` named
    `ad0194bf $113.74` the priciest and totalled `$190.67`; reading the same
    window whole names `717157f5 $146.70` and totals `$486.16`. A command whose
    entire job is "which session should I look inside" cannot answer with the
    first few it happened to open, and `total_usd` must cover the window, not
    the rows that survived the cut — `shown`/`scanned` say which is which.
    """
    import time

    since = time.time() - days * 86400
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    unpriced: set[str] = set()
    for path in sessions_for(project):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            continue
        session = parse(path, detail=False)
        if not session.turns:
            continue
        parse_subagents(session, detail=False)
        unpriced |= session.unpriced
        priced = [t.cost_usd for t in session.turns if t.cost_usd is not None]
        sub_priced = [t.cost_usd for a in session.subagents for t in a.turns
                      if t.cost_usd is not None]
        rows.append({
            "session": session.session_id,
            "project": path.parent.name,
            "at": session.turns[-1].timestamp,
            "turns": len(session.turns),
            "output": sum(t.output_tokens for t in session.turns),
            "peak_context": max((t.prefix for t in session.turns), default=0),
            "subagents": len(session.subagents),
            # `if priced or sub_priced`: a parent whose own turns are all on an
            # unknown model still has real, known subagent spend to report.
            "cost_usd": (round(sum(priced) + sum(sub_priced), 4)
                         if priced or sub_priced else None),
        })
    rows.sort(key=lambda r: r["cost_usd"] or 0.0, reverse=True)
    total = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    if unpriced:
        warnings.append("no published rate on file for: " + ", ".join(sorted(unpriced))
                        + " — those sessions show no cost and are absent from the total")
    return {
        "days": days,
        "scanned": len(rows),
        "shown": min(limit, len(rows)),
        "sessions": rows[:limit],
        "total_usd": round(sum(total), 4) if total else None,
        "warnings": warnings,
    }
