"""The narrow shop window in front of the full command surface.

`mb` grew to 23 top-level commands / 60 leaves, which is an honest reflection
of what the engine does and a bad first screen for a human: the four commands
used daily sit in the same flat list as `uninstall` and `reflect`. This module
keeps ONE interface and narrows what it shows.

Two rules make that safe:

* **Groups rewrite argv, they never re-implement.** `mb service daemon start`
  becomes `mb daemon start` before the parser sees it, so a grouped command and
  its direct path are the same code path — they cannot drift, and the 1878
  existing tests, 33 skills and 3 hooks that call the direct paths keep working
  untouched.
* **Help is generated from the parser.** Descriptions come from the `help=` text
  already declared in `_build_parser()`, so a renamed command cannot leave a
  stale line here. What this module owns is placement, not prose.

`test_cli_facade.py` asserts the invariant in both directions: every parser
command is either on the window or inside a group, and every name listed here
exists in the parser.
"""
from __future__ import annotations

import argparse
from typing import NamedTuple
from collections.abc import Iterable

# The window, in reading order. Names only — the one-line descriptions are
# pulled from the parser so the two can never disagree.
WINDOW: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Memory", ("search", "learn", "note", "ingest")),
    ("Health", ("status", "doctor")),
    ("Knowledge care", ("curator",)),
    ("Local model", ("llm",)),
    ("Machine", ("service", "setup")),
)

# A *group* is a name that is not a command: it exists only to front others.
# A command belongs to exactly one group; `test_cli_facade` fails on an overlap,
# because two homes means a coin flip every time someone looks for it.
GROUPS: dict[str, tuple[str, ...]] = {
    "service": ("daemon", "index", "model", "watch", "backup", "selftest"),
    "setup": ("config", "project", "hooks", "init", "uninstall"),
}

# A *host* is different: a real command that additionally fronts siblings doing
# the same job under other names. `mb curator` still runs curator — only the
# names listed here are rewritten, and none of them was a valid curator/status
# subcommand before, so no existing invocation changes meaning.
#
# The distinction earns its keep: naming the group `curate` would have invented
# a second name for `curator`, which is already written in 16 skill files, the
# SessionStart hint and the vault's own notes. One name per thing, even when the
# second one reads better.
HOSTS: dict[str, tuple[str, ...]] = {
    "curator": ("digest", "nightly"),
    # Reports that answer "what is in the memory" rather than "is it healthy":
    # they hang off `status` instead of taking two more slots on the window.
    "status": ("stats", "reflect"),
}

GROUP_BLURB: dict[str, str] = {
    "service": "daemon, index, model, watcher, backups, selftest",
    "setup": "config, projects, hooks, install/uninstall",
    "curator": "vault health, gaps, contradictions, digest, nightly",
}

# Kept as a name because it reads better at the call sites below.
STATUS_REPORTS: tuple[str, ...] = HOSTS["status"]

# Separate CLIs installed by the same package. Named once, so `mb --help` is not
# the only place in the system that pretends they do not exist.
NEIGHBOURS: tuple[str, ...] = ("imgqa", "design-scout", "pattern-scout",
                               "component-intake", "reel", "golden", "factory")

_GLOBAL_OPTS_WITH_VALUE = ("--vault",)


class Plan(NamedTuple):
    """What `main()` should do with this argv."""
    action: str                 # "run" | "help" | "group" | "greeting" | "error"
    argv: list[str]             # rewritten argv, for action == "run"
    payload: str | None = None   # group name, "all" for full help, or a message


def command_index(argv: Iterable[str]) -> int | None:
    """Position of the first positional token — the command — or None.

    Global options may precede it (`mb --vault /x service daemon start`), so the
    scan skips flags and the one option that takes a separate value.
    """
    argv = list(argv)
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_OPTS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        return i
    return None


def vault_of(argv: Iterable[str]) -> str | None:
    """`--vault` value from a raw argv, for the paths that never reach argparse."""
    argv = list(argv)
    for i, tok in enumerate(argv):
        if tok == "--vault" and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith("--vault="):
            return tok.split("=", 1)[1]
    return None


def resolve(argv: Iterable[str]) -> Plan:
    """Map a raw argv onto either a rewritten argv or a facade-owned action."""
    argv = list(argv)
    idx = command_index(argv)

    if idx is None:
        # Nothing but flags. `-h/--help/--version` still belong to argparse;
        # a bare `mb` gets the greeting.
        if any(t in ("-h", "--help") for t in argv):
            return Plan("help", argv)
        if any(t == "--version" for t in argv):
            return Plan("run", argv)
        return Plan("greeting", argv)

    head, cmd, tail = argv[:idx], argv[idx], argv[idx + 1:]

    if cmd == "help":
        return Plan("help", argv, "all" if "--all" in tail or "-a" in tail else None)

    if cmd in GROUPS:
        members = GROUPS[cmd]
        if not tail or tail[0] in ("-h", "--help"):
            return Plan("group", argv, cmd)
        if tail[0] not in members:
            return Plan("error", argv,
                        f"mb {cmd}: unknown subcommand {tail[0]!r}\n"
                        f"available: {', '.join(members)}")
        return Plan("run", head + tail)

    if cmd in HOSTS and tail and tail[0] in HOSTS[cmd]:
        # A hosted sibling: `mb curator nightly` -> `mb nightly`. Anything else
        # after a host — its own subcommands, its flags, nothing at all — is
        # left for the parser exactly as the user typed it.
        return Plan("run", head + tail)

    return Plan("run", argv)


# --------------------------------------------------------------------------
# help rendering — descriptions come from the parser, placement from WINDOW
# --------------------------------------------------------------------------

def _subparsers_action(parser: argparse.ArgumentParser):
    for action in parser._actions:                      # noqa: SLF001 — argparse
        if isinstance(action, argparse._SubParsersAction):   # noqa: SLF001
            return action
    return None


def parser_commands(parser: argparse.ArgumentParser) -> dict[str, str]:
    """{command: one-line help} straight from the built parser."""
    action = _subparsers_action(parser)
    if action is None:
        return {}
    return {choice.dest: (choice.help or "")
            for choice in action._choices_actions}          # noqa: SLF001


def subcommands_of(parser: argparse.ArgumentParser, command: str) -> dict[str, str]:
    """{subcommand: help} for one command, e.g. every verb under `daemon`.

    The parser declares subcommands two ways — `add_subparsers` (model, backup,
    config …) and a positional with `choices` (daemon, index) — and a reader of
    the help should not have to care which. The positional style carries no
    per-verb help text, so those values are empty strings.
    """
    action = _subparsers_action(parser)
    if action is None or command not in action.choices:
        return {}
    child = action.choices[command]
    nested = parser_commands(child)
    if nested:
        return nested
    for act in child._actions:                              # noqa: SLF001
        if not act.option_strings and act.choices:
            return {str(choice): "" for choice in act.choices}
    return {}


def _describe(name: str, commands: dict[str, str]) -> str:
    """Window blurb where one is declared, the parser's own help otherwise.

    A host like `curator` gets the blurb because on the window it stands for
    itself *and* what it fronts; its bare `--help` still shows the parser text.
    """
    return GROUP_BLURB.get(name) or commands.get(name, "")


def render_help(parser: argparse.ArgumentParser, full: bool = False) -> str:
    commands = parser_commands(parser)
    # The usage line stays, and it takes the program name from the parser rather
    # than a literal: it is what tells a user reading a shim-launched help that
    # they are looking at `mb` and not `_launch.py` (test_bin_shims).
    lines = [f"usage: {parser.prog} <command> [options]", "",
             "Singularity memory brain.", ""]

    for title, names in WINDOW:
        lines.append(title)
        for name in names:
            lines.append(f"  {name:<9} {_describe(name, commands)}")
        lines.append("")

    if not full:
        lines += [
            "  mb help --all         every command, including direct paths",
            "  mb <command> --help   options for one command",
            "",
            "Alongside mb (separate CLIs): " + ", ".join(NEIGHBOURS),
        ]
        return "\n".join(lines)

    def _entry(name: str) -> str:
        subs = subcommands_of(parser, name)
        suffix = "  " + "|".join(subs) if subs else ""
        return f"    {name:<11}{commands.get(name, '')}{suffix}"

    lines.append("Groups in full")
    for group, members in GROUPS.items():
        lines.append(f"  mb {group}")
        lines += [_entry(member) for member in members]
    lines.append("")

    lines.append("Commands that front their siblings")
    for host, members in HOSTS.items():
        lines.append(f"  mb {host}   {commands.get(host, '')}")
        lines += [_entry(member) for member in members]
    lines += [
        "",
        "Every grouped and fronted command also answers on its direct path "
        "(mb daemon status == mb service daemon status).",
        "",
        "Alongside mb (separate CLIs): " + ", ".join(NEIGHBOURS),
    ]
    return "\n".join(lines)


def render_group(parser: argparse.ArgumentParser, group: str) -> str:
    commands = parser_commands(parser)
    lines = [f"usage: {parser.prog} {group} <command> [options]", "",
             GROUP_BLURB.get(group, ""), ""]
    for member in GROUPS[group]:
        subs = subcommands_of(parser, member)
        lines.append(f"  {member:<10} {commands.get(member, '')}")
        if subs:
            lines.append(f"  {'':<10} {'|'.join(subs)}")
    lines += ["",
              f"Direct paths keep working: mb {GROUPS[group][0]} ... "
              f"== mb {group} {GROUPS[group][0]} ..."]
    return "\n".join(lines)


def render_greeting(snapshot: dict) -> str:
    """One line of state for a bare `mb`, then where to go next.

    A bare command should orient, not act — and orientation here is "is the
    memory answering", which is exactly what the status snapshot already knows.
    """
    index = snapshot.get("index") or {}
    daemon = snapshot.get("daemon") or {}
    parts = []
    if index.get("present"):
        age = index.get("age_hours")
        parts.append(f"{index.get('chunks')} chunks"
                     + (f", {age}h old" if age is not None else ""))
    else:
        parts.append(index.get("error") or "no index")
    parts.append("daemon up" if daemon.get("ping") else "daemon down")
    return ("memory: " + " · ".join(parts) + "\n"
            "mb --help for the commands, mb status for the full snapshot")
