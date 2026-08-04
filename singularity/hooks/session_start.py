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
  - digest nag (T12): top-3 actionable merge candidates from the cached
    .mb/digest-last.json (mb nightly), threshold + 72h rate-gated via
    .mb/nag-state.json; stale/missing cache -> silent or one bounded line,
    never a crash, never a stamp write on an empty/failed show
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

STALE_CACHE_HOURS = 48   # T12: older digest-last.json -> one bounded line, no action nag
# W11: how many recorded open questions before the ingest nag mentions them.
# A constant rather than a config key — it tunes one line of prose, and the
# vault already carries ten knobs.
OPEN_QUESTIONS_NAG_THRESHOLD = 5


def _nag_due(state_path, key: str, rate_limit_h: float) -> bool:
    """Shared rate gate. Each nag owns its own key in `.mb/nag-state.json` so
    showing one never resets the other's clock."""
    if not state_path.exists():
        return True
    try:
        last = json.loads(state_path.read_text(encoding="utf-8")).get(key)
    except (OSError, json.JSONDecodeError):
        return True
    if not last:
        return True
    try:
        age_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
    except (TypeError, ValueError):
        return True
    return age_h >= rate_limit_h


def _stamp_nag(state_path, key: str) -> None:
    """Merge-and-write, never clobber: two nags share this file."""
    state = {}
    if state_path.exists():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            state = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError):
            state = {}
    state[key] = datetime.now().isoformat(timespec="seconds")
    tmp = state_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(state_path)


def build_context(event_cwd: str | None = None) -> str:
    from singularity.config import read_config_tolerant, resolve_vault

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
    from singularity.hooks_state import (
        CAPTURE_HOOKS_FRAGMENT,
        VAULT_WRITE_POLICY_FRAGMENT,
    )
    lines.append(CAPTURE_HOOKS_FRAGMENT)
    lines.append(VAULT_WRITE_POLICY_FRAGMENT)

    if not vault.exists():
        lines.append("WARNING: vault path does not exist on this machine.")
        return "\n".join(lines)

    light_cfg = read_config_tolerant()

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

    # T12: cached digest-actions nag — file-only, threshold + 72h rate-gated,
    # top-3 concrete actions. Fail-open: a broken/missing cache must never
    # break the session, and a suppressed/empty nag must never write a stamp.
    try:
        digest_last = vault / ".mb" / "digest-last.json"
        if digest_last.exists():
            age_h = (time.time() - digest_last.stat().st_mtime) / 3600
            data = json.loads(digest_last.read_text(encoding="utf-8"))
            threshold = float(light_cfg.get("digest_score_threshold", 0.45))
            entries = data.get("entries", [])
            actionable = [e for e in entries
                         if e.get("candidates") and e["candidates"][0]["score"] >= threshold]
            if age_h > STALE_CACHE_HOURS:
                lines.append(f"mb nightly cache stale ({age_h:.0f}h old) — digest nag "
                             "suppressed until nightly runs again.")
            elif actionable:
                nag_state_path = vault / ".mb" / "nag-state.json"
                rate_limit_h = float(light_cfg.get("nightly_nag_rate_limit_hours", 72))
                if _nag_due(nag_state_path, "last_shown_at", rate_limit_h):
                    top = sorted(actionable, key=lambda e: -e["candidates"][0]["score"])[:3]
                    actions = "; ".join(
                        f"{e['file']}#{e['section']} -> {e['candidates'][0]['file']} "
                        f"({e['candidates'][0]['score']:.2f})" for e in top)
                    lines.append(f"digest backlog ({len(actionable)} actionable): {actions} "
                                "— `mb digest plan` / `mb digest mark`")
                    _stamp_nag(nag_state_path, "last_shown_at")
    except Exception:  # noqa: BLE001 — a broken digest cache must never break the session
        pass

    # W11: ingest nag — un-swallowed sources and recorded open questions.
    # Reads `.mb/sources.json` directly (small, always current) rather than the
    # nightly snapshot, so a machine that has never run nightly still gets the
    # reminder; open-question counts DO come from the snapshot because they
    # need a vault scan this hook must not do.
    try:
        unprocessed = 0
        registry_path = vault / ".mb" / "sources.json"
        if registry_path.exists():
            data = json.loads(registry_path.read_text(encoding="utf-8-sig"))
            entries = data.get("sources") if isinstance(data, dict) else None
            if isinstance(entries, dict):
                unprocessed = sum(1 for r in entries.values()
                                  if isinstance(r, dict) and r.get("status") == "new")
        open_questions = 0
        gaps_last = vault / ".mb" / "gaps-last.json"
        if gaps_last.exists() and \
                (time.time() - gaps_last.stat().st_mtime) / 3600 <= STALE_CACHE_HOURS:
            gaps_data = json.loads(gaps_last.read_text(encoding="utf-8"))
            counts = gaps_data.get("counts") if isinstance(gaps_data, dict) else None
            if isinstance(counts, dict):
                open_questions = int(counts.get("open_questions") or 0)

        if unprocessed > 0 or open_questions > OPEN_QUESTIONS_NAG_THRESHOLD:
            nag_state_path = vault / ".mb" / "nag-state.json"
            rate_limit_h = float(light_cfg.get("nightly_nag_rate_limit_hours", 72))
            if _nag_due(nag_state_path, "ingest_last_shown_at", rate_limit_h):
                parts = []
                if unprocessed:
                    parts.append(f"{unprocessed} ingested source(s) not turned into "
                                 f"notes yet — `mb ingest list --status new` then "
                                 f"`mb ingest plan <id>`")
                if open_questions > OPEN_QUESTIONS_NAG_THRESHOLD:
                    parts.append(f"{open_questions} recorded open question(s) — "
                                 f"`mb curator gaps`")
                lines.append("ingest backlog: " + "; ".join(parts))
                _stamp_nag(nag_state_path, "ingest_last_shown_at")
    except Exception:  # noqa: BLE001 — a broken registry must never break the session
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
