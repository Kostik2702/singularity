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

from .config import Config, validate_config

CLAUDE_SETTINGS = "settings.json"
GEMINI_SETTINGS = "settings.json"
AFTER_AGENT_HOOK_NAME = "singularity-after-agent"
GROK_HOOK_FILE = "singularity.json"

# T02: single shared natural-language contract, injected verbatim into the
# Claude SessionStart context, the Codex ~/.codex/AGENTS.md managed block and
# the Gemini ~/.gemini/GEMINI.md managed block — one source so the three
# clients cannot drift apart on what "stop hooks" means or how to act on it.
CAPTURE_HOOKS_FRAGMENT = """## Capture/stop hooks control
User intent (RU or EN) like "включи/выключи/покажи stop hooks", "enable/disable/status
capture hooks", "включи/выключи auto-capture" means ONLY the capture/stop reminder hook —
never SessionStart/recall and never any guard hook. Run the deterministic CLI, never
hand-edit settings.json:
```
mb hooks status [--json]
mb hooks enable
mb hooks disable
```
After enable/disable, always run `mb hooks status` and report the effective state back.
A vague "выключи все хуки" / "disable all hooks" does NOT mean capture hooks by default —
confirm scope with the user before running a command."""

# VSO-01: the same one-source treatment for WHERE knowledge is written.
# Incident 2026-07-13 ([[Error - Vault staging duplicates]]): a client edited 14
# vault documents as copies under the user's profile root and copied them back,
# because the sandbox exposed that directory as writable and a mirror looked
# like the path of least resistance. No unique data was lost — every staging
# file turned out byte-identical to, or contained in, its canonical version —
# but for a while the machine had two working sets of the same notes, and only
# a hash audit could say which was authoritative. A rule saying "artifacts live
# in the vault" already existed; it was not present at the moment the write
# path got chosen, which is the only moment it could have helped.
VAULT_WRITE_POLICY_FRAGMENT = """## Vault is the write destination, not a copy of it
Before the FIRST write of anything destined for the vault, resolve the destination:
- Write **directly** to the vault path (or through `mb note new` / `mb learn`).
- **Never** create an editable mirror, staging copy or scratch draft of a vault
  document elsewhere and copy it back — not for an editor, a sandbox, or a patch
  tool. A second working copy is a second source of truth until someone diffs them.
- External folders are for INPUT (reading source material) and for an export the
  user explicitly asked for. Neither authorizes an editable duplicate.
- If a direct vault write is genuinely blocked (permission, sandbox), STOP and say
  so, then ask for access or a vault-native path. Do not route around it.
Reading vault files, and copying INTO the vault, are always fine."""


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
    """Desired-state of the `auto_capture` flag, read directly from
    vault/.mb/config.json (independent of the CLI's stricter load_config gate,
    so this stays usable from a doctor/status probe on a broken config)."""
    data, error = _read_json_tolerant(vault / ".mb" / "config.json")
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
