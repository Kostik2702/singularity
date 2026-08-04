"""Every path-valued entry in a vault config, and whether it belongs to this OS.

The vault is designed to travel: plain Markdown in git, no proprietary
database, and `W3-sources-portability` explicitly checks that `.mb/sources`
moves with it. `.mb/config.json` is versioned in that same repository — so when
the vault is cloned onto a different machine, every absolute path inside it
arrives too, and on a different OS every one of them is wrong (issue #14).

`mb doctor` validated exactly one of them (`code_repos_dir`, via `K1`) and said
nothing about `llm.server_bin`, the `gguf` paths, the `--chat-template-file`
buried inside `llm.models.*.args`, or any `projects[].path`. The install read
as essentially healthy while local-LLM delegation and every project brain were
dead — failures that would surface one at a time, mid-session, weeks apart.

Two questions are worth separating, which is why `foreign_os()` exists at all:
a path that is merely missing might be a typo or an unmounted drive, but a path
matching `C:\\…` on a POSIX host is not missing — it is from another operating
system, and saying so once beats reporting nine confusing absences.
"""
from __future__ import annotations

import os
import re
from typing import Any

# Machine-specific top-level keys. `_PATH_KEYS` in config.py covers the same
# two for schema validation; this module is about a different question (does
# the value belong to THIS machine), so the list is repeated deliberately
# rather than imported and quietly widened by a future schema change.
TOP_LEVEL_PATH_KEYS = ("code_repos_dir", "hf_cache")

_WINDOWS_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_POSIX_ABS = re.compile(r"^/[^/]")           # not "//server/share" (UNC)


def _looks_like_a_path(value: str) -> bool:
    """Absolute, and with structure past the root.

    Used only for `llm.models.*.args`, where paths sit among ordinary flags and
    numbers. A bare `-ngl` or `99` must not be reported as a missing file."""
    if not (_WINDOWS_ABS.match(value) or _POSIX_ABS.match(value)):
        return False
    return "/" in value[2:] or "\\" in value[2:]


def foreign_os(value: str) -> str | None:
    """`"windows"` / `"posix"` when this path is absolute for the OTHER OS."""
    if os.name == "nt":
        return "posix" if _POSIX_ABS.match(value) else None
    return "windows" if _WINDOWS_ABS.match(value) else None


def path_entries(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Every path-valued entry as (dotted key, value), in a stable order.

    Nested shapes are enumerated explicitly rather than by walking for
    "anything that looks like a path": a heuristic sweep over the whole config
    would pick up `skip_path_parts`, `immutable_roots` and `personal_roots`,
    which are vault-RELATIVE by design and must never be reported as missing
    absolute paths."""
    out: list[tuple[str, str]] = []

    def add(key: str, value: Any) -> None:
        if isinstance(value, str) and value.strip():
            out.append((key, value))

    for key in TOP_LEVEL_PATH_KEYS:
        add(key, data.get(key))

    llm = data.get("llm")
    if isinstance(llm, dict):
        add("llm.server_bin", llm.get("server_bin"))
        models = llm.get("models")
        if isinstance(models, dict):
            for name, spec in models.items():
                if not isinstance(spec, dict):
                    continue
                add(f"llm.models.{name}.gguf", spec.get("gguf"))
                args = spec.get("args")
                if isinstance(args, list):
                    for i, arg in enumerate(args):
                        if isinstance(arg, str) and _looks_like_a_path(arg):
                            out.append((f"llm.models.{name}.args[{i}]", arg))

    projects = data.get("projects")
    if isinstance(projects, dict):
        for pid, spec in projects.items():
            if isinstance(spec, dict):
                add(f"projects.{pid}.path", spec.get("path"))

    return out
