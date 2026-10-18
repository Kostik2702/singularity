"""Capture/stop hooks desired/installed/effective state (T01, GCLI/fork-port).

stdlib-only, no model/daemon/network import — safe to call from the CLI (and
later doctor/status, T02) without pulling in heavy dependencies. Mirrors the
tolerant JSON read already used by singularity.hooks.capture_decision so the
same fail-open behavior is visible here as `source: "error"` instead of a
silent default.

Scope: capture hooks only (Claude Stop, Gemini AfterAgent, Grok Stop).
SessionStart and any future guard hooks are untouched by every function in
this module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ..config import Config, validate_config

CLAUDE_SETTINGS = "settings.json"
GEMINI_SETTINGS = "settings.json"
AFTER_AGENT_HOOK_NAME = "singularity-after-agent"
GROK_HOOK_FILE = "singularity.json"

# T02: single shared natural-language contract, injected verbatim into the
# Claude SessionStart context, the Codex ~/.codex/AGENTS.md managed block and
# the Gemini ~/.gemini/GEMINI.md managed block — one source so the three
# clients cannot drift apart on what "stop hooks" means or how to act on it.
CAPTURE_HOOKS_FRAGMENT = """## Capture/stop hooks control
A request to enable, disable or show the status of the stop / capture / auto-capture
hooks — in whatever language it is phrased — means ONLY the capture/stop reminder
hook, never SessionStart/recall and never any guard hook. Run the deterministic CLI,
never hand-edit settings.json:
```
mb hooks status [--json]
mb hooks enable
mb hooks disable
```
After enable/disable, always run `mb hooks status` and report the effective state back.
A vague "turn off all the hooks" does NOT mean the capture hooks by default —
confirm scope with the user before running a command."""

# One fragment, not two. `MEMORY_WRITES_FRAGMENT` and `VAULT_WRITE_POLICY_FRAGMENT`
# both told the reader how something reaches the vault, arrived in the same payload,
# and disagreed on the one case that matters: the first said a new note is
# `mb note new`, the second opened with "write **directly** to the vault path".
# `mb-memory/SKILL.md` and the guard side with the first. Two texts at one decision
# point is exactly what [[Rule - a rule is spoken at the point of decision]] forbids,
# and the counter-text wins by default — so the fix is one text.
#
# Incident behind the staging half (2026-07-13, [[Error - Vault staging duplicates]]):
# 14 vault documents were edited as copies under the user's profile root and copied
# back, because the sandbox exposed that directory as writable. No unique data was
# lost, but for a while the machine had two working sets and only a hash audit could
# say which was authoritative.
MEMORY_WRITES_FRAGMENT = """## The vault: what it is, and how anything gets into it
- "memory" / "remember this" / `um` from the user, in any language = THE VAULT, not
  the harness-local store. "Commit the memory" = commit and push it.
- The vault holds KNOWLEDGE. Rules, checklists and skill instructions are mechanism
  and ship with the engine — never make the machine's behaviour depend on which
  vault is configured.
- Resolve the destination BEFORE the first write. New file in a managed root —
  `mb note new <type> "<title>"`, always: it brings the template, `source:` and
  routing, and the guard flags a hand-made one. Insight — `mb learn`. Config —
  `mb config set`. Editing an EXISTING note with Edit/Write is the normal path.
- Never build an editable mirror, staging copy or draft of a vault document elsewhere
  and copy it back — a second working copy is a second source of truth until someone
  diffs them. External folders are INPUT, or an export that was asked for. Blocked
  from writing? STOP and say so, then ask for a vault-native path.
- Searching is a command too: `mb search` over grep, `mb curator gaps` over walking
  links. Reading vault files, and copying INTO the vault, are always fine."""

VERIFICATION_FRAGMENT = """## Green is not the same as verified
- Take the exit code from the COMMAND, never from a pipe:
  `cmd > run.log 2>&1; echo "EXIT=$?"` (or `${PIPESTATUS[0]}`). `cmd | tail; echo $?`
  reports `tail`'s status, so a run that never started looks green.
- A new test-invariant ("no cycles", "the domain must not print") is proven by
  PLANTING the violation it must catch and watching the suite go red — in the
  form the code actually uses. A green invariant on healthy code is
  indistinguishable from one that checks nothing.
- A check that can `skip` itself when its target is missing must fail instead;
  after a refactor compare the `skipped` count with the previous run, because a
  rise means checks switched themselves off.
"""

# The failure this catches is the one the whole rule set kept repeating: a rule
# is written down, and then absent at the moment it could have acted. Six
# defects in a single day (v0.6.1→v0.7.1) had that anatomy. It belongs in the
# main thread because that is where rules get authored and where "just fix the
# inconsistency" gets decided.
RULE_POINT_FRAGMENT = """## A rule only acts where the action is chosen
- Wrote a rule? Name the decision point and put the text THERE (skill file,
  agent definition, hook fragment, default in code). A shared list of rules is
  an archive, not a mechanism. Several points → ONE source, injected into each,
  plus a test that fails when any copy disappears.
- Check the counter-text: if the instruction already standing at that point
  contradicts the new rule, the instruction wins and the rule is decoration.
- Before "fixing" an inconsistency you found, look for a RECORDED decision —
  grep test names (`*_cannot_*`, `*_never_*`), their comments, `docs/`, ADRs. In
  mature code an oddity is more often deliberate than accidental. Disagree with
  what you find? Say so to the user; never silently overrule it."""

# The main thread has no preamble, and §2's `No mid-flight stops` is written for
# subagents — correctly, because a subagent has no channel to the user and asking
# would strand it. The main thread does have one, so the same uncertainty has a
# different right answer there. Putting the two in one place would leave two texts
# arguing at one point; they are split by surface instead.
#
# The cost of not having it, measured: a benchmark pack whose own task budgets sum
# to 276 minutes was handed over with a 23-minute deadline. The ratio was legible
# on the first read and went unsaid until asked about afterwards.
THINK_FIRST_FRAGMENT = """## Name the fork before working, not after
- A request that admits several readings leading to materially different work:
  say them and pick one out loud BEFORE starting. Silently choosing is the
  failure — not choosing wrong. Mark it where the work will be read
  (`ASSUMED <x> from <source>`).
- Say plainly when scope, budget or deadline do not fit the goal, and when a
  simpler approach solves the stated problem. Noticing later costs the attempt.
- The boundary is WHEN, not who. `No mid-flight stops` — preamble §2 and the
  council skills — governs work already in flight: once agents are spawned, take
  a marked default and never pause a round. This governs the moment before that,
  while there is still someone to ask."""


MEASURED_AXIS_FRAGMENT = """## Measure the axis you were ASKED about
- The person says the result is wrong a SECOND time while your instruments say
  it works: stop producing instruments. They are answering a different question.
  Name the axis you measured, name the axis they asked about, and go measure
  theirs.
- Before reporting "X works", ask whether X is the word THEY used for the goal or
  the word you used for your work. If the second, report your work and say plainly
  that it proves nothing about their goal.
- Green tools plus an unhappy person is almost never "they misunderstood" — it is
  two different axes under one word. The thing that breaks the deadlock is looking
  at the artefact itself (open the file, render the frame), not another log."""


TEAM_LEAD_FRAGMENT = """## Delegating to executors
- You choose model and effort for each executor (skill `team-lead`): haiku — reading, search, mechanics; sonnet — code by a plan (the default when no model is named); opus — checks and thinking, always named. Do it yourself when the brief would cost as much as the work; 2–4 executors, not dozens.
- Background executors; big results come back as a file path. Fable only via `advisor` with `ПОЧЕМУ FABLE:`.
- A council starts only on the owner's command — ask for it.
- Told the budget is spent: stop and ask the owner."""


def _read_json_tolerant(path: Path) -> tuple[dict[str, Any], str | None]:
    """(data, error). Missing file -> ({}, None). Malformed/non-object -> ({}, reason)."""
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    if not isinstance(data, dict):
        return {}, "root is not a JSON object"
    return data, None


def capture_config_state(vault: Path) -> dict[str, Any]:
    """Desired-state of the `auto_capture` flag, read directly from the machine
    config (independent of the CLI's stricter load_config gate, so this stays
    usable from a doctor/status probe on a broken config).

    `vault` is still taken because a pre-0.7 vault-resident config is honoured
    until the migration runs — same upgrade window `read_config_tolerant()`
    covers, and for the same reason: this decides whether the capture hook
    fires, and defaulting it during an upgrade would silently change
    behaviour."""
    from ..config import CONFIG_FILE, legacy_config_path

    path = CONFIG_FILE
    machine, machine_error = _read_json_tolerant(path)
    configured = machine_error is None and any(
        k not in ("schema_version", "vault_path") for k in machine)
    if not configured and legacy_config_path(vault).exists():
        path = legacy_config_path(vault)
    data, error = _read_json_tolerant(path)
    if error is not None:
        return {"configured": True, "default": True, "source": "error", "error": error}
    value = data.get("auto_capture", True)
    if not isinstance(value, bool):
        return {"configured": True, "default": True, "source": "error",
                "error": f"auto_capture: expected a boolean, got {type(value).__name__}"}
    source = "explicit" if "auto_capture" in data else "default"
    return {"configured": value, "default": True, "source": source}


def _claude_installed() -> bool:
    """True if a Singularity-owned Stop entry exists. Ownership is the `_mb`
    marker (current install/register_hooks.py contract) OR, for installs that
    predate the marker, a command referencing our owned stop_capture.py — a
    real machine was found with exactly this legacy unmarked shape, so this
    is not speculative."""
    data, error = _read_json_tolerant(Path.home() / ".claude" / CLAUDE_SETTINGS)
    if error is not None:
        return False
    stop = data.get("hooks", {}).get("Stop", [])
    if not isinstance(stop, list):
        return False
    for entry in stop:
        if not isinstance(entry, dict):
            continue
        if entry.get("_mb"):
            return True
        for h in entry.get("hooks", []):
            if isinstance(h, dict) and "stop_capture.py" in str(h.get("command", "")):
                return True
    return False


def _gemini_installed() -> bool:
    data, error = _read_json_tolerant(Path.home() / ".gemini" / GEMINI_SETTINGS)
    if error is not None:
        return False
    after_agent = data.get("hooks", {}).get("AfterAgent", [])
    if not isinstance(after_agent, list):
        return False
    for entry in after_agent:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if isinstance(h, dict) and h.get("name") == AFTER_AGENT_HOOK_NAME:
                return True
    return False


def _grok_installed() -> bool:
    """True if the Singularity-owned Stop hook file exists in the Grok config
    root ($GROK_HOME else ~/.grok — the same resolver register_grok uses) and
    carries our grok_stop_capture.py command."""
    env = os.environ.get("GROK_HOME", "").strip()
    grok_home = Path(env) if env else Path.home() / ".grok"
    data, error = _read_json_tolerant(grok_home / "hooks" / GROK_HOOK_FILE)
    if error is not None:
        return False
    stop = data.get("hooks", {}).get("Stop", [])
    if not isinstance(stop, list):
        return False
    for entry in stop:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks", []):
            if isinstance(h, dict) and "grok_stop_capture.py" in str(h.get("command", "")):
                return True
    return False


def client_states(vault: Path) -> dict[str, dict[str, Any]]:
    """Per-client installed/supported/mode/effective. `effective` additionally
    requires the desired auto_capture flag to be on — installed-but-disabled
    is reported honestly, not as effective."""
    enabled = capture_config_state(vault)["configured"]
    claude_installed = _claude_installed()
    gemini_installed = _gemini_installed()
    grok_installed = _grok_installed()
    return {
        "claude": {"installed": claude_installed, "supported": True, "mode": "block",
                   "effective": claude_installed and enabled},
        "gemini": {"installed": gemini_installed, "supported": True, "mode": "remind",
                   "effective": gemini_installed and enabled},
        # Codex never receives a runtime capture hook (install/register_codex.py
        # deploys only AGENTS.md + skill junctions) — always unsupported, not probed.
        "codex": {"installed": False, "supported": False, "mode": "none", "effective": False},
        # Grok Stop hook mirrors Claude's block-mode gate (GRK-00 proved the
        # Claude-compatible stop-gate vocabulary; GRK-04 shipped the adapter).
        "grok": {"installed": grok_installed, "supported": True, "mode": "block",
                 "effective": grok_installed and enabled},
    }


def status(vault: Path) -> dict[str, Any]:
    cfg_state = capture_config_state(vault)
    warnings: list[str] = []
    if cfg_state["source"] == "error":
        warnings.append(f"config.json unreadable, treating as default enabled: {cfg_state['error']}")
    return {
        "kind": "capture_hooks",
        "configured": cfg_state["configured"],
        "default": cfg_state["default"],
        "source": cfg_state["source"],
        "clients": client_states(vault),
        "warnings": warnings,
    }


def set_capture_enabled(cfg: Config, enabled: bool) -> None:
    """Atomically write auto_capture=<enabled>, validated, preserving every
    other key (foreign or known). Raises ValueError with the actionable
    validate_config message on failure — caller decides how to print it."""
    candidate = {**cfg.data, "auto_capture": enabled}
    result = validate_config(candidate)
    if result["errors"]:
        raise ValueError("; ".join(result["errors"]))
    cfg.data = candidate
    cfg.save()
