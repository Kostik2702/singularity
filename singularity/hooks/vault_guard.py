"""Claude PreToolUse hook: vault guard (T08).

Flags NEW-file creation in a managed knowledge root and direct writes to
`.mb/config.json` — both bypass the sanctioned CLI, which never touches a
client's Write/Edit tool at all (see singularity/vault_guard.py for why no
spoofable command-detection is needed).

On by default since 2026-08-04 (owner decision), in `audit` mode: the check
only adds additionalContext — NEVER permissionDecision, never blocks. It was
opt-in until then, and the reason for flipping it is what the default cost:
a note written with a client's Write tool skips `templates/`, so `source:` and
the required sections are absent, and nothing says so for weeks. `deny`
(explicit opt-in) sets permissionDecision="deny" with an actionable reason;
that half is unchanged per the resolved Open Decision (2026-07-12) — audit
first, deny only once explicitly configured.

Turning it off stays one key away (`vault_guard_enabled: false`), and an
install that has ever saved its config already carries an explicit value, so
upgrading changes nothing there.

ONE exception (WIKI-LOOP D6, 2026-07-28): `immutable_roots` (default
`[".mb/sources"]`) always denies, audit or not. The real immutability
guarantee remains the write-once check in `singularity/sources.py` — this is
the second line, not the first, and it is now armed by default rather than
waiting to be switched on.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# hook runs outside the package context; make the package importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_GUARDED_TOOLS = {"Write", "Edit", "MultiEdit"}


def evaluate(event: dict) -> dict | None:
    """Returns the full hookSpecificOutput payload dict, or None for a
    silent no-op. Never raises: every failure path returns None; the caller
    (main) still wraps this call defensively as a second line of defense."""
    if not isinstance(event, dict) or event.get("tool_name") not in _GUARDED_TOOLS:
        return None
    tool_input = event.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if not isinstance(file_path, str) or not file_path.strip():
        return None

    from singularity.config import (
        DEFAULT_CONFIG,
        config_unreadable,
        read_config_tolerant,
        resolve_vault,
    )

    vault = resolve_vault(None)
    if vault is None or not vault.exists():
        return None

    # A config that exists but does not parse says nothing about what the user
    # wants, and this check is on by default — so acting on the default here
    # would annotate writes on the strength of a guess. Stay silent instead;
    # `mb doctor` is where a broken config gets reported.
    if config_unreadable(vault):
        return None

    # `read_config_tolerant` applies no defaults, so every fallback here has to
    # come from DEFAULT_CONFIG rather than a literal: a second copy of a default
    # is a second thing to forget, and this hook decides whether a check runs at
    # all. `tests/test_vault_guard_hook.py` pins the two together.
    # (No `vault` argument since 0.7: the config is per machine, so there is one
    # file to read and no vault to locate first.)
    light_cfg = read_config_tolerant()
    if not light_cfg.get("vault_guard_enabled",
                         DEFAULT_CONFIG["vault_guard_enabled"]):
        return None

    cwd = event.get("cwd")
    base = Path(cwd) if isinstance(cwd, str) and cwd.strip() else Path.cwd()
    fp = Path(file_path)
    try:
        abs_file = (fp if fp.is_absolute() else base / fp).resolve()
    except (OSError, ValueError):
        return None

    from singularity.machine import vault_guard

    # WIKI-LOOP D6: immutable roots are checked FIRST and are NOT softened by
    # `vault_guard_mode: audit`. Every other check below keeps the 2026-07-12
    # resolution ("audit first"); this one guards the only data under .mb/ that
    # a rebuild cannot reconstruct, so a warning would be useless after the fact.
    imm_root = vault_guard.immutable_root(
        vault, abs_file, light_cfg.get("immutable_roots",
                                       vault_guard.DEFAULT_IMMUTABLE_ROOTS))
    if imm_root is not None:
        return {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"'{imm_root}/' is immutable: ingested sources are the evidence "
                f"other notes cite, and there is no second copy. To record a new "
                f"version run `mb ingest add` (it stores a new source and links "
                f"supersedes/superseded_by); to get the text out use "
                f"`mb ingest export <dir>`.")}}

    if vault_guard.is_config_json(vault, abs_file):
        reason = ("Direct edit of .mb/config.json bypasses validation — use "
                  "`mb config set` / `mb project add` / `mb hooks enable` instead.")
    else:
        root = vault_guard.new_file_managed_root(vault, abs_file)
        if root is None:
            return None
        reason = (f"New file created directly in managed knowledge root "
                  f"'{root}/', bypassing the sanctioned CLI — prefer "
                  f"`mb note new` / `mb learn` so it gets project binding and dedup.")

    mode = light_cfg.get("vault_guard_mode", DEFAULT_CONFIG["vault_guard_mode"])
    if mode == "deny":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": reason}}
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": f"[vault-guard audit] {reason}"}}


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        event = json.loads(raw or "{}")
    except (OSError, json.JSONDecodeError, AttributeError):
        print("{}")
        return 0

    try:
        output = evaluate(event)
    except Exception:  # noqa: BLE001 — a broken guard must never block a tool call
        output = None

    print(json.dumps(output, ensure_ascii=False) if output else "{}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
