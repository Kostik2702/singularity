"""Normalized session activity + platform-neutral capture-decision core (T09).

Replaces the `tool_uses`/`captured` abstraction (raw tool-count threshold,
JSON-substring "mb learn" search on an escaped transcript line) that
singularity.hooks.capture_decision currently uses. This module is 100%
platform-neutral: it has no knowledge of Claude's or Gemini's transcript
wire format at all — a client parser (T10) builds a SessionActivity from its
own transcript, and everything here operates on that normalized structure or
on already-resolved/parsed values, never on raw JSON text.

T09 intentionally does NOT wire this into stop_capture.py/gemini_after_agent.py
— that swap is T10's job, adapting each client's real parser. This module is
purely additive until then.

Convention for T10's parsers (documented here since this is the single
source of the contract, even though no parser lives in this file yet):
  - `code_edits`/`vault_writes`/`sanctioned_captures` count only tool calls
    the transcript confirms SUCCEEDED. A tool-use entry whose success cannot
    be determined (missing/ambiguous success field) must NOT be counted —
    conservative under-counting, never over-counting — and must add a
    message to `parse_warnings` instead of guessing.
  - Any transcript-parsing exception is the parser's responsibility to catch
    and fail open (emit an activity with a warning, never raise into the
    hook's main()), matching every other hook's existing fail-open pattern.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# `\b` alone is not enough after "learn"/"new" — it matches on a hyphen too
# ("mb learn-report" is NOT `mb learn`), so the command word must be followed
# by whitespace or end-of-string, not just any non-word character.
_SANCTIONED_CAPTURE_RE = re.compile(r"(?:^|[\s;&|])mb\s+(?:learn|note\s+new)(?=\s|$)")


@dataclass
class SessionActivity:
    """Normalized summary of one client session, built by a T10 parser from
    its own transcript format. Every count is a successful-tool-call count,
    never a raw total (no raw tool-count threshold, T09 acceptance)."""
    client: str
    session_id: str
    project_id: Optional[str] = None
    code_edits: int = 0
    vault_writes: int = 0
    sanctioned_captures: int = 0
    parse_warnings: list[str] = field(default_factory=list)


@dataclass
class Decision:
    action: str    # "nudge" | "none"
    reason: str


def is_code_edit_path(resolved_path: Path, cwd: Path, vault: Optional[Path]) -> bool:
    """A code edit is a successful write whose resolved target lies inside
    the session's cwd (its project/working tree, spec §6.4 point 2 — no
    registered-project lookup required just to COUNT as a code edit; that's
    a separate concern for which project_id to stamp) and OUTSIDE the vault
    (a vault-targeted write is a vault write, not a code edit, even if the
    vault happens to be inside cwd). `resolved_path`/`cwd`/`vault` must
    already be resolved Path objects from a PARSED tool-input field — never
    call this against a raw transcript string. Path comparison relies on
    pathlib's own platform-aware equality (case-insensitive on Windows)."""
    try:
        resolved = resolved_path.resolve()
        cwd_r = cwd.resolve()
    except (OSError, ValueError):
        return False
    try:
        resolved.relative_to(cwd_r)
    except ValueError:
        return False
    if vault is not None:
        try:
            resolved.relative_to(vault.resolve())
            return False
        except (OSError, ValueError):
            pass
    return True


def is_vault_write_path(resolved_path: Path, vault: Path) -> bool:
    """A vault write is a successful write whose resolved target lies inside
    the vault — counts toward `captured`, independent of `mb learn`/`mb note
    new` (spec §6.4 point 3: "a successful vault write OR sanctioned
    capture"). Same resolved-Path-only contract as is_code_edit_path."""
    try:
        resolved_path.resolve().relative_to(vault.resolve())
        return True
    except (OSError, ValueError):
        return False


def is_sanctioned_capture_command(command: str) -> bool:
    """True if an already-PARSED shell command string (the real value of a
    Bash tool's `input.command` field, never a raw JSON line) invokes
    `mb learn` or `mb note new`. Anchored on a preceding boundary
    (start-of-string or shell separator) and a following word boundary so it
    cannot match `mb learning-notes` or a substring inside an unrelated
    argument — the escaped-substring heuristic T09 explicitly replaces."""
    return bool(_SANCTIONED_CAPTURE_RE.search(command))


def decide(enabled: bool, activity: SessionActivity, stop_hook_active: bool,
          marker_exists: bool) -> Decision:
    """Pure decision core — no config/marker/transcript I/O here; the caller
    (a T10 client adapter) reads config, checks the marker file, and parses
    the transcript into `activity` BEFORE calling this. Never raises for a
    well-typed SessionActivity; a parser-level failure is the caller's
    fail-open responsibility (module docstring)."""
    if stop_hook_active:
        return Decision("none", "stop_hook_active: already retrying, never stack nudges")
    if not enabled:
        return Decision("none", "capture disabled")
    if marker_exists:
        return Decision("none", "already nudged once this session")
    if activity.vault_writes > 0 or activity.sanctioned_captures > 0:
        return Decision("none", "session already captured")
    if activity.code_edits <= 0:
        return Decision("none", "no successful code edit this session")
    return Decision("nudge", f"{activity.code_edits} uncaptured code edit(s) this session")
