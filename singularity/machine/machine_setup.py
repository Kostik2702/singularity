"""What THIS machine still has to state, and the command that states it.

The v0.9.0 config is per machine because a vault travels and every absolute
path inside it is wrong on arrival. That solves the wrong-value problem and
creates a quieter one: an arriving machine now has a config with the *values*
missing rather than wrong, and nothing ever asked for them. A user who
upgrades sees the migration succeed, the doctor go green, and then discovers
weeks later that the code index was never built because nobody said where the
repositories are.

So the moment we know a machine just adopted or created a config — migration
and install — we print what is unstated and the exact command for each. Every
item here is genuinely optional: a machine that never uses local models is not
misconfigured, it is configured differently. That is why this is a prompt at
the two moments it is actionable, not a standing warning that trains people to
ignore warnings.

`doctor A8-machine-setup` reports the same list as information, and warns in
exactly one case: a config that states NOTHING beyond which vault to use —
a setup that stopped halfway.
"""
from __future__ import annotations

from typing import Any

# (key, what it is for, how to state it). Order is the order a new machine
# most likely wants them in.
ITEMS: list[tuple[str, str, str]] = [
    ("code_repos_dir",
     "where your repositories live — the code index reads it",
     'mb config set code_repos_dir "<path>"'),
    ("projects",
     "the project registry — per-project brains and session context",
     "mb project add <name> <path>"),
    ("llm",
     "local llama-server and the models it serves",
     "mb llm setup --dir <path>   (then `mb llm add <name> <gguf>`)"),
    ("imgqa_ml_venv",
     "the isolated ML venv `imgqa cutout` dispatches to",
     'mb config set imgqa_ml_venv "<path>"'),
]


def _is_stated(key: str, value: Any) -> bool:
    """Present is not enough: an empty registry states nothing.

    `{}` and `[]` are what a half-finished setup leaves behind, and treating
    them as answers is how "configured" and "mentioned once" get confused."""
    if value is None:
        return False
    if isinstance(value, (dict, list, str)):
        return bool(value)
    return True


def unstated(data: dict) -> list[dict[str, str]]:
    """The items this machine has not answered, in ITEMS order."""
    return [{"key": key, "what": what, "how": how}
            for key, what, how in ITEMS
            if not _is_stated(key, data.get(key))]


def stated(data: dict) -> list[str]:
    return [key for key, _what, _how in ITEMS if _is_stated(key, data.get(key))]


def summary_lines(data: dict, *, prefix: str = "[mb] ") -> list[str]:
    """Human-facing block for migration and install. Empty when nothing is
    unstated — silence is the right output for a machine that answered."""
    missing = unstated(data)
    if not missing:
        return []
    lines = [f"{prefix}this machine has not stated "
             f"{len(missing)} thing(s) — none is required, "
             f"but nothing will use them until it does:"]
    for item in missing:
        lines.append(f"{prefix}  {item['key']}: {item['what']}")
        lines.append(f"{prefix}      {item['how']}")
    return lines
