"""Which parser and resolver wrote a graph. Parser-free on purpose: the query
path checks these before deciding whether it may update a graph in place,
and must not import tree-sitter to do it (import-floor guard).
"""
from __future__ import annotations

import importlib.util
from typing import Any

# importlib.metadata is imported where it is used: ~16 ms at import time, paid
# by every `mb code` and `mb search --graph` that never compares versions.

# Bumped on any change that can move an edge: a graph built by resolver N is
# never extended by resolver N+1 (UNMET o). 2: name-unique skips test files and
# function-local definitions (independent audit of P1a output, 2026-09-26).
# 3: TypeScript / JavaScript resolution (P1b). 4: TS name-unique hardened after
# the P1b audit (getters, call-chain receivers, unreachable packages); function-
# typed interface properties become callable members. 5: Python star imports
# bounded by the hop limit (a star cycle recursed without end; a chain of more
# than _MAX_HOPS star re-exports no longer resolves).
# 6: Java and Kotlin, one `jvm` family (CODE-GRAPH D3).
RESOLVER_VERSION = 6
PARSER_PACKAGES = ("tree-sitter", "tree-sitter-python", "tree-sitter-typescript",
                   "tree-sitter-javascript", "tree-sitter-java", "tree-sitter-kotlin")
_MODULES = ("tree_sitter", "tree_sitter_python", "tree_sitter_typescript", "tree_sitter_javascript",
            "tree_sitter_java", "tree_sitter_kotlin")


def parser_versions() -> dict[str, str]:
    """Recorded in every graph. A writer whose versions differ is not allowed
    to mix its output into this graph (UNMET o) -- see `version_mismatch`."""
    from importlib.metadata import PackageNotFoundError, version
    out = {}
    for pkg in PARSER_PACKAGES:
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "absent"
    out["resolver"] = str(RESOLVER_VERSION)
    return out


def version_mismatch(meta: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """{package: (recorded, running)} for every version that differs."""
    recorded = meta.get("versions") or {}
    running = parser_versions()
    return {k: (recorded.get(k, "absent"), v) for k, v in running.items() if recorded.get(k) != v}


def parser_available() -> bool:
    """The parser wheels are importable here -- found, not imported."""
    return all(importlib.util.find_spec(m) is not None for m in _MODULES)
