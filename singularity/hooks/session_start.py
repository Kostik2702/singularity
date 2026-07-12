"""SessionStart hook: inject memory context into every Claude Code session.

Port of Genevra session-start-context.sh (auto-recall level of the memory
design). MUST be fast: file reads only, no model, no daemon round-trip.

Emits hookSpecificOutput.additionalContext with:
  - date/time + vault location + mb quick reference
  - project brain (T06): project resolved from the hook event's cwd (falling
    back to process cwd, then "general"), budgeted brain-note excerpt from
    .mb/context.json
  - active tasks (learnings/active/*)
  - latest changelog entry heading
  - doctor warnings from the last cached run (.mb/doctor-last.json)
  - [T11/T12 will add a cached digest-actions block here]
The whole injection is capped by `session_context_max_lines` (T06 total
budget) on top of each block's own truncation.
Self-heal: silently restarts the search daemon / watcher jobs is NOT done
here (too slow) — doctor-last warnings surface the problem instead.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# hook runs outside the package context; make the package importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _light_config(vault: Path) -> dict:
    """Tolerant, dependency-free read of .mb/config.json — NOT the full
    validated Config/load_config() path (too slow and it can sys.exit on a
    malformed config, which a hook must never do). Missing/malformed -> {};
    every caller already treats absence as "use the default"."""
    cfg_path = vault / ".mb" / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_context(event_cwd: str | None = None) -> str:
    from singularity.config import resolve_vault

    vault = resolve_vault(None)
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    if vault is None:
        lines.append(f"[Singularity memory] {now}. No vault configured on this "
                     "machine (set MB_HOME or create ~/.singularity.json).")
        return "\n".join(lines)
    lines.append(f"[Singularity memory] {now}. Vault: {vault}")
    lines.append(
        "Semantic search over the vault: `mb search \"<query>\"` "
        "(RU/EN, ~20ms warm; flags: --top N --type T --tags a,b --links X --json). "
        "SEM-FIRST: use it BEFORE grep/Read for any knowledge discovery. "
        "Capture insights: `mb learn \"<insight>\" --task <ID>`; new notes: `mb note new <type> <title>`."
    )
    from singularity.hooks_state import CAPTURE_HOOKS_FRAGMENT
    lines.append(CAPTURE_HOOKS_FRAGMENT)

    if not vault.exists():
        lines.append("WARNING: vault path does not exist on this machine.")
        return "\n".join(lines)

    light_cfg = _light_config(vault)

    # T06: project brain — independent fail-open block. Project detection
    # never touches SessionStart's own operation even if it errors.
    try:
        from singularity import projects as projects_mod
        from singularity.context_map import DEFAULT_BRAIN_MAX_LINES, project_brain_block

        registry = light_cfg.get("projects")
        registry = registry if isinstance(registry, dict) else {}
        cwd = event_cwd or os.getcwd()          # event cwd first, else process cwd
        project = projects_mod.detect_project(registry, cwd)   # None -> "general"

        max_lines = light_cfg.get("brain_max_lines", DEFAULT_BRAIN_MAX_LINES)
        brain = project_brain_block(vault, project, max_lines)
        if brain:
            lines.append(brain)
    except Exception:  # noqa: BLE001 — a broken brain note must never break the session
        pass

    # pending compaction plans (resume offers) — one-shot files, see compaction-plan skill
    plans_dir = vault / "compaction-plans"
    if plans_dir.exists():
        plans = sorted(plans_dir.glob("*.md"))
        if plans:
            names = ", ".join(p.name for p in plans[:3])
            lines.append(
                f"PENDING compaction plans ({len(plans)}): {names} — "
                "ask the user whether to resume one (read it fully, then delete the plan file).")

    # active tasks (learnings/active)
    active_dir = vault / "learnings" / "active"
    if active_dir.exists():
        tasks = sorted(active_dir.glob("*.md"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:5]
        if tasks:
            names = ", ".join(p.stem for p in tasks)
            lines.append(f"Active tasks (learnings/active): {names}")

    # latest changelog heading
    changelog_dir = vault / "changelog"
    if changelog_dir.exists():
        logs = sorted(changelog_dir.glob("*.md"), reverse=True)
        if logs:
            for line in logs[0].read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("## "):
                    last_heading = line[3:].strip()
            try:
                lines.append(f"Latest memory changelog entry: {last_heading}")
            except NameError:
                pass

    # doctor warnings (cached, cheap)
    doctor_last = vault / ".mb" / "doctor-last.json"
    if doctor_last.exists():
        age_h = (time.time() - doctor_last.stat().st_mtime) / 3600
        try:
            data = json.loads(doctor_last.read_text(encoding="utf-8"))
            bad = [c for c in data.get("checks", [])
                   if c.get("status") in ("FAIL", "WARN")]
            if bad:
                brief = "; ".join(f"{c['id']}={c['status']}" for c in bad[:5])
                lines.append(f"mb doctor ({age_h:.0f}h ago): {brief} — run `mb doctor --fix`")
        except json.JSONDecodeError:
            pass

    # T06: total injection budget on top of each block's own truncation.
    text = "\n".join(lines)
    budget = light_cfg.get("session_context_max_lines", 200)
    if isinstance(budget, int) and budget > 0:
        all_lines = text.splitlines()
        if len(all_lines) > budget:
            text = "\n".join(all_lines[:budget]) + \
                f"\n... (session context truncated at session_context_max_lines={budget})"
    return text


def main() -> int:
    # Windows console defaults stdout to cp1251 → Cyrillic becomes mojibake;
    # Claude Code reads hook stdout as UTF-8 (twin of the stdin utf-8-sig rule)
    sys.stdout.reconfigure(encoding="utf-8")
    event_cwd = None
    try:
        raw = sys.stdin.buffer.read().decode("utf-8-sig", errors="ignore").strip()
        if raw:
            event = json.loads(raw)
            if isinstance(event, dict):
                cwd = event.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    event_cwd = cwd
    except (OSError, json.JSONDecodeError, AttributeError):
        pass       # T06: missing/malformed event cwd falls back to process cwd
    try:
        context = build_context(event_cwd)
    except Exception as exc:  # noqa: BLE001 — a broken hook must never block the session
        context = f"[Singularity memory] hook error: {exc}"
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
