"""What a subagent must have DONE before its report is accepted.

A "mandatory" line in an agent prompt was followed in 5-10 % of runs; the
transcript is the only witness that cannot be talked around, so the check reads
it. Pure logic: the one I/O is reading the transcript file it is handed. The
parser is conservative like the Stop hook's: a line it cannot classify is
skipped, never guessed, and any read failure means "no verdict" (fail open).
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

CONTRACTS: dict[str, tuple[str, ...]] = {
    "investigator": ("preamble", "memory_search", "confidence"),
    "reviewer": ("preamble", "memory_search", "confidence"),
    "critic": ("preamble", "memory_search", "confidence"),
    "validator": ("preamble", "memory_search", "confidence"),
    "historian": ("preamble", "memory_search", "confidence"),
    "coder": ("preamble", "diff_after_edit"),
    "web-implementer": ("preamble", "diff_after_edit"),
    "web-viewer": ("preamble",),
    "scout": ("preamble",),
}

_SHELL_TOOLS = {"Bash", "PowerShell"}
_PREAMBLE = "AGENT_SHARED_PREAMBLE.md"
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# `mb search` only where a command starts: not inside another command's argument.
# A command starts at the line start, after a separator or an opening bracket, after
# `do`/`then`, or right after the quote of `bash -lc "`; env/timeout/cd prefixes and a
# path to the binary may sit in between.
_MB_SEARCH = re.compile(
    r"(?:^|[;&|\n({]|\b(?:do|then)\b|-\w*c\s+[\"'])\s*"
    r"(?:\w+=\S+\s+|timeout\s+\S+\s+|cd [^;&\n]+[;&]+\s*)*"
    r"(?:\S*/)?mb(?:\s+--[\w-]+(?:[= ]\S+)?)*\s+search\b")
# Engine files stay ASCII: the dashes and the Cyrillic reasons are escapes.
_SUMMARY_LINE = re.compile(
    r"\b\d{1,3}\s?%[*_]*"
    r"(?:\s*(?:\u2014|\u2013|-{1,2}|\()\s*|\s+)[*_]*"
    "(?:because\\b|\u043f\u043e\u0442\u043e\u043c\u0443\\s+\u0447\u0442\u043e"
    "|\u0442\u0430\u043a\\s+\u043a\u0430\u043a|\u0442\\.\u043a\\.)",
    re.IGNORECASE)
_GIT_VIEW = re.compile(
    r"\bgit\b(?:\s+-[Cc]\s+\S+|\s+--[\w-]+(?:=\S+)?)*\s+(?:diff|status)\b")


@dataclass
class Run:
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    report: str = ""
    cwd: str = ""


def parse(transcript: Path, last_message: str = "") -> Run:
    """Every assistant tool call in order, and the text a report can live in."""
    run = Run()
    reports: list[str] = []
    last_text = ""
    with open(transcript, encoding="utf-8", errors="ignore") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "assistant":
                continue
            message = obj.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            texts: list[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use" and isinstance(block.get("name"), str):
                    tool_input = block.get("input")
                    tool_input = tool_input if isinstance(tool_input, dict) else {}
                    run.tool_calls.append((block["name"], tool_input))
                    if block["name"] == "SubagentHandback" and isinstance(
                            tool_input.get("message"), str):
                        reports.append(tool_input["message"])
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
            last_text = "\n".join(texts)
    run.report = "\n".join([*reports, last_text, last_message])
    return run


def _shell_commands(run: Run) -> list[tuple[int, str]]:
    return [(i, str(inp.get("command", ""))) for i, (name, inp) in enumerate(run.tool_calls)
            if name in _SHELL_TOOLS]


def _preamble(run: Run) -> bool:
    """Read through the Read tool or through the shell: on real transcripts most
    agents `cat`/`sed` the file, and a Read-only check called 38 of 66 of them
    non-compliant when they were not."""
    return any((name == "Read" and str(inp.get("file_path", "")).endswith(_PREAMBLE))
               or (name in _SHELL_TOOLS and _PREAMBLE in str(inp.get("command", "")))
               for name, inp in run.tool_calls)


def _memory_search(run: Run) -> bool:
    return any(_MB_SEARCH.search(cmd) for _, cmd in _shell_commands(run))


def _confidence(run: Run) -> bool:
    skill = any(name == "Skill" and str(inp.get("skill", "")).split(":")[-1]
                == "confidence-calibration" for name, inp in run.tool_calls)
    return skill and _SUMMARY_LINE.search(run.report) is not None


def _inside_cwd(run: Run, tool_input: dict) -> bool:
    """A write outside the repo (a scratch file) is never in `git diff`."""
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not run.cwd or not isinstance(path, str) or not path:
        return True
    if not Path(path).is_absolute():
        return True
    return Path(path).resolve().is_relative_to(Path(run.cwd).resolve())


def _diff_after_edit(run: Run) -> bool:
    edits = [i for i, (name, inp) in enumerate(run.tool_calls)
             if name in _EDIT_TOOLS and _inside_cwd(run, inp)]
    if not edits:
        return True
    return any(i > edits[-1] and _GIT_VIEW.search(cmd) for i, cmd in _shell_commands(run))


CHECKS: dict[str, Callable[[Run], bool]] = {
    "preamble": _preamble,
    "memory_search": _memory_search,
    "confidence": _confidence,
    "diff_after_edit": _diff_after_edit,
}

HOW: dict[str, str] = {
    "preamble": "Read the shared preamble (AGENT_SHARED_PREAMBLE.md next to your agent file): "
                "sections 1-2, and 4-5 before returning.",
    "memory_search": 'Search the memory before concluding: `mb search "<the gist of the task>"` '
                     "and say in the report what it returned (nothing relevant is a valid answer).",
    "confidence": "Compute confidence with the `confidence-calibration` skill (never by feel) and "
                  "put the Summary Line `X% — because <key evidence>` (or the same in the "
                  "report's language) in the report.",
    "diff_after_edit": "You edited files: run `git diff --stat` now, check that exactly the "
                       "planned files changed, and attach its output to the report.",
}


def missing(agent_type: str, transcript: Path, last_message: str = "",
            cwd: str = "") -> list[str] | None:
    """Unsatisfied checks in contract order; [] = read and compliant (or no
    contract); None = no verdict could be reached. Fails OPEN: a hook must never
    trap an agent because a file could not be read."""
    contract = CONTRACTS.get(agent_type)
    if not contract:
        return []
    try:
        run = parse(Path(transcript), last_message)
        run.cwd = cwd
        return [name for name in contract if not CHECKS[name](run)]
    except Exception:  # noqa: BLE001 - no verdict beats a wrong one
        return None


def reason(names: list[str]) -> str:
    lines = [f"[Singularity contract] Your report is not accepted yet: {len(names)} required "
             "step(s) are missing. Do them, then return the full report again."]
    lines += [f"{i}. {HOW[name]}" for i, name in enumerate(names, 1)]
    lines.append("This check runs once; it will not stop you a second time.")
    return "\n".join(lines)
