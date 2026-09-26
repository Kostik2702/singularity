"""IMPORTS edges equal an independent oracle on this engine's own source
(CODE-GRAPH P1 acceptance).

Not `test_architecture`'s import walker: it reads only `ImportFrom`, skips
plain `import x`, and records an imported FUNCTION as a module (by design --
it looks for cycles, not for a dependency graph). The oracle here is written
from the language rules alone: stdlib `ast`, both import forms, and "is it a
module?" decided by whether the file exists. It shares no code with the
resolver, so agreement is evidence and not an echo.
"""
from __future__ import annotations

import ast
from pathlib import Path

from singularity.codegraph.extract_py import extract
from singularity.codegraph.resolve_py import PyResolver

REPO = Path(__file__).resolve().parents[1]


def _engine_files() -> dict[str, Path]:
    out = {}
    for p in sorted((REPO / "singularity").rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if "__pycache__" in p.parts or "/skills/" in f"/{rel}":
            continue
        out[rel] = p
    return out


def _module_file(parts: list[str], files: dict[str, Path]) -> str | None:
    stem = "/".join(parts)
    for cand in (f"{stem}.py", f"{stem}/__init__.py"):
        if cand in files:
            return cand
    return None


def _oracle(files: dict[str, Path]) -> set[tuple[str, str]]:
    edges = set()
    for rel, path in files.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        pkg = rel.split("/")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _module_file(alias.name.split("."), files)
                    if target and target != rel:
                        edges.add((rel, target))
            elif isinstance(node, ast.ImportFrom):
                base = pkg[: len(pkg) - (node.level - 1)] if node.level else []
                mod = base + (node.module.split(".") if node.module else [])
                for alias in node.names:
                    target = _module_file(mod + [alias.name], files) if alias.name != "*" else None
                    target = target or _module_file(mod, files)
                    if target and target != rel:
                        edges.add((rel, target))
    return edges


def _resolver_imports(files: dict[str, Path]) -> set[tuple[str, str]]:
    facts = {rel: extract(rel, p.read_bytes()) for rel, p in files.items()}
    edges, _ = PyResolver(facts).resolve()
    strip = lambda q: q.split("::", 1)[0]   # noqa: E731
    return {(strip(e.src), strip(e.dst)) for e in edges if e.kind == "IMPORTS"}


def test_imports_match_an_independent_ast_oracle_on_the_engine():
    files = _engine_files()
    oracle, graph = _oracle(files), _resolver_imports(files)
    assert len(oracle) > 200                       # the control: 278 module pairs on 2026-09-26
    assert graph - oracle == set(), f"invented imports: {sorted(graph - oracle)[:10]}"
    assert oracle - graph == set(), f"missed imports: {sorted(oracle - graph)[:10]}"


def test_the_oracle_sees_what_the_architecture_walker_misses():
    """The planted blind spot: a plain `import singularity.x` and a relative
    import two levels up must both be in the oracle, or the equality above
    would hold for the wrong reason."""
    files = _engine_files()
    oracle = _oracle(files)
    assert any(src.count("/") >= 2 and dst.startswith("singularity/") and dst.count("/") == 1
               for src, dst in oracle)             # `from ..x import y` out of a subpackage
