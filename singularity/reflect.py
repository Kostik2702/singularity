"""Offline, privacy-safe adoption telemetry (T14).

Walks each client's COMPLETED session transcripts and counts session-level
adoption signals. Never persists prompt text, tool-call arguments, excerpts,
or transcript file paths -- only counts and short labels that are already
public elsewhere (skill names from install/skill_manifest.py, client/tool
names). No model, no daemon, no network call.

Population & denominator (written down per the T14 acceptance criterion --
the task's own AI prompt requires proving the denominator does not exclude
successful invocations):

  Population = every TOP-LEVEL (non-subagent) session transcript across all
  supported clients, modified within the [--days N] window, regardless of
  whether it used any Singularity feature at all.

  "Top-level" is a STRUCTURAL classification, confirmed live against this
  machine's real transcripts (not guessed, not taken from documentation):
    - Claude: a *.jsonl file directly under ~/.claude/projects/<project>/ --
      never one found inside a sibling <session-id>/subagents/ directory.
      Every event in such a file carries isSidechain:false; every event in
      a subagents/*.jsonl file carries isSidechain:true (verified against
      this machine's real ~/.claude/projects tree). Both signals are used:
      the walker never even lists subagents/ directories, and the parser
      additionally skips any event where isSidechain is True, belt-and-
      suspenders.
    - Gemini: every ~/.gemini/tmp/<project>/chats/session-*.jsonl file.
      Gemini CLI (0.50.0, per tests/fixtures/gemini/README.md) has no
      subagent/sidechain concept in its transcript format at all -- nothing
      to exclude beyond the glob itself.
    - Grok: every <grok-home>/sessions/<encoded-cwd>/<session-id>/ directory
      (grok-home = $GROK_HOME else ~/.grok, the CLI's own resolution order)
      holding a chat_history.jsonl AND a parseable summary.json WITHOUT a
      `session_kind` field. Subagent children are real session dirs in the
      same tree carrying `"session_kind": "subagent"` (verified live on
      grok 0.2.111, tests/fixtures/grok/session_summary_subagent.json) --
      that marker is the exclusion signal. Forked/resumed sessions get their
      own session-id directory (summary carries parent_session_id) and are
      counted once each, like any other top-level session. A session dir
      whose summary.json is missing/unreadable cannot PROVE top-level
      identity -- it is skipped and counted as malformed, never guessed
      into the population.
    - Codex: excluded entirely. Confirmed (T15 investigation, corroborated
      by hooks_state.py's own "Codex never receives a runtime capture hook"
      comment) that Codex has no hook registration and no discoverable
      transcript/session-log anywhere in this integration -- there is
      nothing to walk.

  Denominator = len(population), computed ONCE, before any metric is
  evaluated. Every metric below is a COUNT drawn from members of this SAME
  population -- an adopting session is, by construction, already a member
  of the population (it cannot be counted in a numerator without first
  being counted in the denominator). This directly closes the original
  fork-integration review's MAJOR-1 concern (a pure code_edits>0 capture
  criterion silently lost research sessions, already fixed for the LIVE
  capture-decision gate in T09/T10) for the MEASUREMENT side too:
  `eligible_research` exists specifically so sessions with real knowledge
  work but zero code edits are counted and visible, not invisible.

Metrics (each expressed as count / denominator, i.e. an adoption rate):
  - search_then_read: `mb search` was invoked, and a Read/Grep/Glob
    (Claude) or read_file (Gemini) tool call happened LATER in the same
    session -- search results were actually followed up on, not just fired
    and ignored.
  - vault_touch: at least one successful vault-path read or write.
  - code_edits: at least one successful write outside the vault.
  - eligible_research: code_edits == 0 AND (searched OR a vault read
    happened) -- real knowledge work with no code editing at all.
  - skill_invocation: at least one successful invocation of an
    ENGINE-OWNED skill (name checked against
    install/skill_manifest.SKILL_MANIFEST -- the same registry T15 uses;
    an unrecognized/foreign skill name is never counted and never stored).

Every parser is conservative by the same T09/T10 convention: a tool call
whose success cannot be confirmed, or whose path/command cannot be parsed,
is simply not counted (never guessed), and a malformed transcript is
skipped with a count, never silently dropped without trace.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import session_activity as sa
from .config import Config, atomic_write_json

SNAPSHOT_NAME = "reflect-last.json"
HISTORY_NAME = "reflect-history.jsonl"

_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}
_READ_TOOLS = {"Read", "Grep", "Glob"}
_SHELL_TOOLS = {"Bash", "PowerShell"}

_GEMINI_WRITE_TOOLS = {"write_file", "replace", "edit", "create_file"}

_GROK_WRITE_TOOLS = {"write", "search_replace"}
_GROK_READ_TOOLS = {"read_file", "grep", "list_dir"}
_GROK_SHELL_TOOLS = {"run_terminal_command"}
# grok wraps every tool error in a fixed content prefix (verified live,
# tests/fixtures/grok/chat_history_edit_results.jsonl) -- the ONLY success
# signal chat_history.jsonl offers for non-shell tools.
_GROK_FAILURE_PREFIXES = ("Failed to parse arguments",)

# same anchoring convention as session_activity._SANCTIONED_CAPTURE_RE: a
# preceding shell-separator/start-of-string boundary and a following
# whitespace/end boundary, so `mb search` cannot match `mb searchxyz` or a
# substring inside an unrelated argument.
_SEARCH_RE = re.compile(r"(?:^|[\s;&|])mb\s+search(?=\s|$)")


def is_search_command(command: str) -> bool:
    """True if an already-PARSED shell command string invokes `mb search`."""
    return bool(_SEARCH_RE.search(command))


@dataclass
class ReflectSession:
    client: str
    session_id: str
    project_id: str | None = None
    searched: bool = False
    search_then_read: bool = False
    vault_reads: int = 0
    vault_writes: int = 0
    code_edits: int = 0
    skill_invocations: int = 0
    parse_warnings: list[str] = field(default_factory=list)

    @property
    def vault_touch(self) -> bool:
        return self.vault_reads > 0 or self.vault_writes > 0

    @property
    def eligible_research(self) -> bool:
        return self.code_edits == 0 and (self.searched or self.vault_reads > 0)


def _load_install(name: str):
    """Load a module from install/ (not an importable package) -- same
    pattern doctor.py already uses (_load_install) for the same reason:
    reuse the single source instead of re-deriving/duplicating it."""
    import importlib.util

    engine = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, engine / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# transcript discovery -- structural top-level/subagent split
# ---------------------------------------------------------------------------

def claude_transcript_root() -> Path:
    return Path.home() / ".claude" / "projects"


def gemini_transcript_root() -> Path:
    return Path.home() / ".gemini" / "tmp"


def grok_session_root() -> Path:
    """$GROK_HOME else ~/.grok -- the same resolution order the Grok CLI and
    install/register_grok.py use."""
    env = os.environ.get("GROK_HOME", "").strip()
    return (Path(env) if env else Path.home() / ".grok") / "sessions"


def _claude_session_paths(root: Path, since: float) -> list[Path]:
    """Top-level (non-subagent) transcripts: *.jsonl files directly under a
    project dir. Never descends into a sibling <session-id>/subagents/ dir
    -- confirmed live that subagent transcripts live ONLY there."""
    out: list[Path] = []
    if not root.is_dir():
        return out
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        for p in project_dir.glob("*.jsonl"):
            try:
                if p.stat().st_mtime >= since:
                    out.append(p)
            except OSError:
                continue
    return out


def _gemini_session_paths(root: Path, since: float) -> list[Path]:
    out: list[Path] = []
    if not root.is_dir():
        return out
    for p in root.glob("*/chats/session-*.jsonl"):
        try:
            if p.stat().st_mtime >= since:
                out.append(p)
        except OSError:
            continue
    return out


def _grok_session_paths(root: Path, since: float) -> tuple[list[Path], int]:
    """(top-level session DIRS, malformed count). A dir is in the population
    iff its chat_history.jsonl is inside the window AND its summary.json
    parses AND carries no `session_kind` (subagent children carry
    session_kind="subagent"; anything else unexpected is excluded the same
    conservative way). An unreadable summary is counted as malformed --
    top-level identity is proven, never assumed."""
    out: list[Path] = []
    malformed = 0
    if not root.is_dir():
        return out, malformed
    try:
        groups = [g for g in root.iterdir() if g.is_dir()]
    except OSError:
        return out, malformed
    for group in groups:
        try:
            session_dirs = [s for s in group.iterdir() if s.is_dir()]
        except OSError:
            continue   # one unreadable group must not sink the whole walk
        for sdir in session_dirs:
            history = sdir / "chat_history.jsonl"
            if not history.is_file():
                continue
            try:
                if history.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            try:
                meta = json.loads((sdir / "summary.json").read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                malformed += 1
                continue
            if not isinstance(meta, dict) or meta.get("session_kind") is not None:
                continue   # subagent child (or unknown kind): excluded, not malformed
            out.append(sdir)
    return out, malformed


# ---------------------------------------------------------------------------
# per-client parsers
# ---------------------------------------------------------------------------

def _parse_claude_session(path: Path, vault: Path, known_skills: set[str],
                          registry: dict[str, Any] | None = None) -> ReflectSession:
    activity = ReflectSession(client="claude", session_id=path.stem)
    cwd_path: Path | None = None
    pending: dict[str, tuple[str, dict]] = {}

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("isSidechain") is True:
                    continue   # defensive: dir-level filtering already excludes these

                cwd_val = obj.get("cwd")
                if cwd_path is None and isinstance(cwd_val, str) and cwd_val.strip():
                    cwd_path = Path(cwd_val)
                    # FR-1: telemetry uses the SAME project resolver as every
                    # other command -- resolved once, from the first cwd seen
                    # (Claude Code does not change cwd mid-session).
                    from . import projects as projects_mod
                    activity.project_id = projects_mod.detect_project(registry, cwd_val)

                message = obj.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "tool_use":
                        name = block.get("name")
                        tid = block.get("id")
                        tool_input = block.get("input")
                        if (isinstance(tid, str) and isinstance(tool_input, dict)
                                and (name in _WRITE_TOOLS or name in _READ_TOOLS
                                     or name in _SHELL_TOOLS or name == "Skill")):
                            pending[tid] = (name, tool_input)
                    elif btype == "tool_result":
                        tid = block.get("tool_use_id")
                        if not isinstance(tid, str) or tid not in pending:
                            continue
                        name, tool_input = pending.pop(tid)
                        if block.get("is_error"):
                            continue   # failed call never counts

                        if name in _WRITE_TOOLS:
                            fp = tool_input.get("file_path")
                            abs_path = _resolve_arg_path(fp, cwd_path, path.parent)
                            if abs_path is None:
                                if isinstance(fp, str):
                                    continue
                                activity.parse_warnings.append(f"{name} missing file_path")
                                continue
                            if sa.is_vault_write_path(abs_path, vault):
                                activity.vault_writes += 1
                            elif cwd_path is not None and sa.is_code_edit_path(abs_path, cwd_path, vault):
                                activity.code_edits += 1
                        elif name in _READ_TOOLS:
                            if activity.searched:
                                activity.search_then_read = True
                            fp = tool_input.get("file_path") or tool_input.get("path")
                            abs_path = _resolve_arg_path(fp, cwd_path, path.parent)
                            if abs_path is not None and sa.is_vault_write_path(abs_path, vault):
                                activity.vault_reads += 1
                        elif name in _SHELL_TOOLS:
                            command = tool_input.get("command")
                            if isinstance(command, str) and is_search_command(command):
                                activity.searched = True
                        elif name == "Skill":
                            skill_name = tool_input.get("skill")
                            if isinstance(skill_name, str) and skill_name in known_skills:
                                activity.skill_invocations += 1
    except OSError:
        activity.parse_warnings.append("transcript unreadable")
    return activity


def _parse_gemini_session(path: Path, vault: Path, known_skills: set[str],
                          registry: dict[str, Any] | None = None) -> ReflectSession:
    # project_id stays None for Gemini: the verified transcript shape
    # (tests/fixtures/gemini/README.md, cross-checked against real
    # ~/.gemini/tmp data on this machine) carries no per-event/per-session
    # absolute cwd to resolve against the registry -- never guessed from the
    # chats/<project-slug>/ directory name, which is not the same namespace
    # as Singularity's registered project ids.
    activity = ReflectSession(client="gemini", session_id=path.stem)

    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "gemini":
                    continue
                calls = obj.get("toolCalls")
                if not isinstance(calls, list):
                    continue
                for tc in calls:
                    if not isinstance(tc, dict) or tc.get("status") != "success":
                        continue
                    name = tc.get("name")
                    args = tc.get("args") if isinstance(tc.get("args"), dict) else {}

                    if name in _GEMINI_WRITE_TOOLS:
                        fp = args.get("file_path") or args.get("path") or args.get("absolute_path")
                        abs_path = _resolve_arg_path(fp, None, path.parent)
                        if abs_path is None:
                            if isinstance(fp, str):
                                continue
                            activity.parse_warnings.append(f"{name} missing path arg")
                            continue
                        if sa.is_vault_write_path(abs_path, vault):
                            activity.vault_writes += 1
                        else:
                            # no per-event cwd recorded in this transcript shape --
                            # best-effort: any non-vault successful write counts as
                            # a code edit (cruder than Claude's cwd-bounded check,
                            # documented limitation, still conservative-undercount
                            # only in the vault-write direction, never the reverse)
                            activity.code_edits += 1
                    elif name == "read_file":
                        if activity.searched:
                            activity.search_then_read = True
                        fp = args.get("file_path") or args.get("path") or args.get("absolute_path")
                        abs_path = _resolve_arg_path(fp, None, path.parent)
                        if abs_path is not None and sa.is_vault_write_path(abs_path, vault):
                            activity.vault_reads += 1
                    elif name == "run_shell_command":
                        command = args.get("command")
                        if isinstance(command, str) and is_search_command(command):
                            activity.searched = True
                    elif name == "activate_skill":
                        skill_name = args.get("name")
                        if isinstance(skill_name, str) and skill_name in known_skills:
                            activity.skill_invocations += 1
    except OSError:
        activity.parse_warnings.append("transcript unreadable")
    return activity


def _grok_result_failed(content: str) -> bool:
    return content.startswith(_GROK_FAILURE_PREFIXES) or (
        content.startswith("Tool `") and "` failed:" in content[:80])


def _parse_grok_session(sdir: Path, vault: Path, known_skills: set[str],
                        registry: dict[str, Any] | None = None) -> ReflectSession:
    """Parse one top-level Grok session dir (chat_history.jsonl + summary.json,
    shapes pinned by tests/fixtures/grok/). project_id comes ONLY from the
    verified summary.json info.cwd field resolved through the shared project
    registry -- never from the URL-encoded directory name. Skill invocations
    stay 0: grok 0.2.111 exposes no skill tool call in chat_history (skills
    are injected as instructions), so there is nothing deterministic to count
    -- documented limitation, never guessed from text."""
    activity = ReflectSession(client="grok", session_id=sdir.name)
    cwd_path: Path | None = None
    del known_skills  # no deterministic skill-invocation signal in this format

    try:
        meta = json.loads((sdir / "summary.json").read_text(encoding="utf-8-sig"))
        cwd_val = meta.get("info", {}).get("cwd") if isinstance(meta, dict) else None
        if isinstance(cwd_val, str) and cwd_val.strip():
            cwd_path = Path(cwd_val)
            from . import projects as projects_mod
            activity.project_id = projects_mod.detect_project(registry, cwd_val)
    except (OSError, json.JSONDecodeError, ValueError):
        activity.parse_warnings.append("summary unreadable")

    pending: dict[str, tuple[str, dict]] = {}
    try:
        with open(sdir / "chat_history.jsonl", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                if row.get("type") == "assistant":
                    for call in row.get("tool_calls") or []:
                        if not isinstance(call, dict):
                            continue
                        name = call.get("name")
                        cid = call.get("id")
                        if not isinstance(cid, str) or name not in (
                                _GROK_WRITE_TOOLS | _GROK_READ_TOOLS | _GROK_SHELL_TOOLS):
                            continue
                        try:
                            args = json.loads(call.get("arguments") or "{}")
                        except json.JSONDecodeError:
                            activity.parse_warnings.append(f"unparseable arguments, id={cid}")
                            continue
                        if isinstance(args, dict):
                            pending[cid] = (name, args)
                elif row.get("type") == "tool_result":
                    cid = row.get("tool_call_id")
                    if not isinstance(cid, str) or cid not in pending:
                        continue
                    name, args = pending.pop(cid)
                    content = row.get("content")
                    if not isinstance(content, str) or _grok_result_failed(content):
                        continue   # failed call never counts

                    if name in _GROK_WRITE_TOOLS:
                        fp = args.get("file_path")
                        abs_path = _resolve_arg_path(fp, cwd_path, sdir)
                        if abs_path is None:
                            if isinstance(fp, str):
                                continue
                            activity.parse_warnings.append(f"{name} missing file_path")
                            continue
                        if sa.is_vault_write_path(abs_path, vault):
                            activity.vault_writes += 1
                        elif cwd_path is not None and sa.is_code_edit_path(abs_path, cwd_path, vault):
                            activity.code_edits += 1
                    elif name in _GROK_READ_TOOLS:
                        if activity.searched:
                            activity.search_then_read = True
                        fp = (args.get("file_path") or args.get("path")
                              or args.get("absolute_path"))
                        abs_path = _resolve_arg_path(fp, cwd_path, sdir)
                        if abs_path is not None and sa.is_vault_write_path(abs_path, vault):
                            activity.vault_reads += 1
                    elif name in _GROK_SHELL_TOOLS:
                        # a shell success is confirmed ONLY by the exit: 0 echo
                        if not content.startswith("exit: 0"):
                            continue
                        command = args.get("command")
                        if isinstance(command, str) and is_search_command(command):
                            activity.searched = True
    except OSError:
        activity.parse_warnings.append("transcript unreadable")
    return activity


def _resolve_arg_path(raw: Any, cwd: Path | None, fallback_base: Path) -> Path | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        p = Path(raw)
        return p if p.is_absolute() else (cwd or fallback_base) / p
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------

def _rate(sessions: list[ReflectSession], pred) -> dict[str, Any]:
    total = len(sessions)
    count = sum(1 for s in sessions if pred(s))
    return {"count": count, "total": total,
            "rate": round(count / total, 3) if total else None}


def reflect(cfg: Config, days: int) -> dict[str, Any]:
    """Compute the adoption snapshot. Never writes anything -- pure read +
    aggregate. See run() for the persisted-snapshot entry point."""
    since = time.time() - days * 86400
    vault = cfg.vault
    registry = cfg.get("projects", {}) or {}
    known_skills = set(_load_install("skill_manifest").SKILL_MANIFEST)

    sessions: list[ReflectSession] = []
    malformed = 0
    parse_warnings = 0

    for p in _claude_session_paths(claude_transcript_root(), since):
        try:
            s = _parse_claude_session(p, vault, known_skills, registry)
        except Exception:  # noqa: BLE001 — one bad transcript must not sink the walk
            malformed += 1
            continue
        sessions.append(s)
        parse_warnings += len(s.parse_warnings)

    for p in _gemini_session_paths(gemini_transcript_root(), since):
        try:
            s = _parse_gemini_session(p, vault, known_skills, registry)
        except Exception:  # noqa: BLE001
            malformed += 1
            continue
        sessions.append(s)
        parse_warnings += len(s.parse_warnings)

    grok_dirs, grok_malformed = _grok_session_paths(grok_session_root(), since)
    malformed += grok_malformed
    for sdir in grok_dirs:
        try:
            s = _parse_grok_session(sdir, vault, known_skills, registry)
        except Exception:  # noqa: BLE001
            malformed += 1
            continue
        sessions.append(s)
        parse_warnings += len(s.parse_warnings)

    by_client: dict[str, int] = {}
    by_project: dict[str, int] = {}
    for s in sessions:
        by_client[s.client] = by_client.get(s.client, 0) + 1
        proj = s.project_id or "general"
        by_project[proj] = by_project.get(proj, 0) + 1

    return {
        "window_days": days,
        "total_sessions": len(sessions),
        "by_client": by_client,
        "by_project": by_project,
        "metrics": {
            "search_then_read": _rate(sessions, lambda s: s.search_then_read),
            "vault_touch": _rate(sessions, lambda s: s.vault_touch),
            "code_edits": _rate(sessions, lambda s: s.code_edits > 0),
            "eligible_research": _rate(sessions, lambda s: s.eligible_research),
            "skill_invocation": _rate(sessions, lambda s: s.skill_invocations > 0),
        },
        "malformed_transcripts": malformed,
        "parse_warnings": parse_warnings,
        "excluded_clients": {
            "codex": "no runtime hook / no discoverable transcript in this integration",
        },
    }


def _append_history(cfg: Config, result: dict[str, Any], now: str) -> int:
    """Append one row per `mb reflect` run; prune rows older than
    reflect_retention_days on every write (same shape as
    nightly._step_trend's history pattern) so the file never grows
    unbounded. Row carries counts/rates only -- no session ids, no paths."""
    path = cfg.mb_dir / HISTORY_NAME
    row = {"ts": now, "window_days": result["window_days"],
          "total_sessions": result["total_sessions"], "by_client": result["by_client"],
          "by_project": result["by_project"],
          "metrics": {k: v["rate"] for k, v in result["metrics"].items()}}

    old_lines: list[str] = []
    if path.exists():
        try:
            old_lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            old_lines = []

    retention_days = float(cfg.get("reflect_retention_days"))
    cutoff = time.time() - retention_days * 86400
    kept: list[str] = []
    for line in old_lines:
        try:
            ts = datetime.fromisoformat(json.loads(line)["ts"]).timestamp()
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue   # drop unparseable rows rather than let them accumulate forever
        if ts >= cutoff:
            kept.append(line)
    kept.append(json.dumps(row, ensure_ascii=False))

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(path)
    return len(kept)


def run(cfg: Config, days: int | None = None) -> dict[str, Any]:
    """Compute the snapshot and persist it: atomic latest snapshot +
    bounded/retained history, exactly the `mb reflect` entry point."""
    window = int(days) if days is not None else int(cfg.get("reflect_window_days"))
    result = reflect(cfg, window)
    now = datetime.now().isoformat(timespec="seconds")
    result["generated_at"] = now
    atomic_write_json(cfg.mb_dir / SNAPSHOT_NAME, result)
    result["history_rows"] = _append_history(cfg, result, now)
    return result
