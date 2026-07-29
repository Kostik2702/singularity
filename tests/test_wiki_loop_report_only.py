"""WIKI-LOOP D5 static gate: the engine does mechanics, skills do semantics.

Two enforcement layers:

1. **Report-only modules contain no write primitive at all.** `ingest_plan`,
   `contradictions` and `gaps` produce candidates; a human or a skill applies
   them. This is checked by walking the AST rather than by convention, so the
   invariant cannot rot the first time someone "just adds a small cache write".

2. **`sources` writes only under `.mb/`.** It is the one WIKI-LOOP module that
   writes at all, and it must never touch a vault note. Checked black-box: run
   the real API against a vault populated with notes and assert nothing outside
   `.mb/` changed.

`sources.export()` is deliberately out of scope for (2): its destination is an
explicit user-supplied directory outside the vault (the D1 escape hatch).
"""
from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from singularity import sources
from singularity.config import Config

PACKAGE = Path(__file__).resolve().parents[1] / "singularity"

# Pure planners: not one write primitive, anywhere.
REPORT_ONLY_MODULES = ["ingest_plan.py", "gaps.py"]

# `contradictions.py` is report-only about NOTES but owns one piece of engine
# state -- the verdict journal `.mb/lint.json`, without which a dismissed false
# positive resurfaces on every run. Exactly the same category as
# `.mb/digest.json`, which `digest.mark` writes. It may therefore use
# `atomic_write_json` and nothing else; the black-box test below pins the
# destination to `.mb/`.
JOURNAL_OWNING_MODULES = {"contradictions.py": {"atomic_write_json"}}

# Attribute calls that write: `x.write_text(...)`, `p.mkdir()`, `os.replace(...)`
FORBIDDEN_ATTRS = {
    "write_text", "write_bytes", "mkdir", "makedirs", "touch", "unlink",
    "rmdir", "rmtree", "remove", "rename", "replace", "copy", "copy2",
    "copyfile", "copytree", "move", "writelines", "symlink_to", "hardlink_to",
    "atomic_write_json", "write_registry", "save",
}
# Bare calls that write: `open(...)` in a write mode
FORBIDDEN_NAMES = {"atomic_write_json", "write_registry"}
_WRITE_MODES = ("w", "a", "x", "+")


def _module_path(name: str) -> Path:
    return PACKAGE / name


def _violations(tree: ast.AST, allowed: set[str] = frozenset()) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_ATTRS \
                and func.attr not in allowed:
            # `.replace` is ambiguous: `Path.replace(dst)` renames a file,
            # `str.replace(old, new)` does not touch disk. Arity separates them.
            if func.attr == "replace" and len(node.args) != 1:
                continue
            found.append(f"line {node.lineno}: .{func.attr}(...)")
        elif isinstance(func, ast.Name):
            if func.id in FORBIDDEN_NAMES and func.id not in allowed:
                found.append(f"line {node.lineno}: {func.id}(...)")
            elif func.id == "open":
                mode = ""
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                    mode = str(node.args[1].value)
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                if any(m in mode for m in _WRITE_MODES):
                    found.append(f"line {node.lineno}: open(..., {mode!r})")
    return found


@pytest.mark.parametrize("module_name", REPORT_ONLY_MODULES)
def test_report_only_modules_never_write(module_name):
    path = _module_path(module_name)
    if not path.exists():
        pytest.skip(f"{module_name} not implemented yet (WIKI-LOOP in progress)")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _violations(tree)
    assert not bad, (
        f"{module_name} must stay report-only (WIKI-LOOP D5) — found write "
        f"call(s): {bad}")


@pytest.mark.parametrize("module_name", sorted(JOURNAL_OWNING_MODULES))
def test_journal_owning_modules_use_only_the_atomic_helper(module_name):
    path = _module_path(module_name)
    if not path.exists():
        pytest.skip(f"{module_name} not implemented yet (WIKI-LOOP in progress)")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    bad = _violations(tree, allowed=JOURNAL_OWNING_MODULES[module_name])
    assert not bad, (
        f"{module_name} may only persist its journal via atomic_write_json "
        f"(WIKI-LOOP D5) — found: {bad}")


def test_contradictions_writes_only_its_journal(tmp_path):
    """Black-box companion to the AST check: scanning writes nothing at all,
    and recording a verdict touches exactly one file under .mb/."""
    from singularity import contradictions

    vault = tmp_path / "vault"
    (vault / "reference").mkdir(parents=True)
    (vault / "reference" / "a.md").write_text(
        "---\ntype: reference\ncreated: 2026-01-01\n---\n\n# A\n\n## S\nbody text\n",
        encoding="utf-8")
    cfg = Config(vault=vault, data={})

    before = _snapshot(vault, exclude="nothing-is-excluded")
    contradictions.scan(cfg)
    assert _snapshot(vault, exclude="nothing-is-excluded") == before, \
        "a scan must be a pure read"

    contradictions.record_verdict(cfg, "a.md#S||b.md#S", "false_positive")
    after = _snapshot(vault, exclude="nothing-is-excluded")
    new_files = set(after) - set(before)
    assert new_files == {".mb/lint.json"}


def _snapshot(root: Path, exclude: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if rel == exclude or rel.startswith(exclude + "/"):
            continue
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def test_sources_writes_only_under_mb(tmp_path):
    """Black-box proof of the real invariant. `sources.py` legitimately READS
    vault notes (backfill scans them for external references) — what it must
    never do is create or modify one, so this exercises every entry point that
    touches the vault and compares the whole tree outside `.mb/`."""
    vault = tmp_path / "vault"
    for rel in ("reference/a.md", "learnings/active/T.md", "decisions/d.md"):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\ntype: reference\nsource: "https://example.com/x"\n---\n\n'
                     f"# {rel}\n\nbody with https://example.com/y in it\n",
                     encoding="utf-8")
    cfg = Config(vault=vault, data={})

    before = _snapshot(vault, exclude=".mb")
    result = sources.add(cfg, "# Ingested\n\nsome external document body",
                         origin_ref="D:/docs/x.md")
    sources.add(cfg, "# Ingested\n\nsome external document body", origin_ref="D:/docs/x.md")
    sources.list_sources(cfg)
    sources.get(cfg, result["source_id"])
    sources.read_source_text(cfg, result["source_id"])
    sources.attach_notes(cfg, result["source_id"], ["reference/a.md"], log=True)
    sources.backfill(cfg)                    # dry run
    sources.backfill(cfg, apply=True)        # writes only the registry
    sources.sweep_log(cfg)
    after = _snapshot(vault, exclude=".mb")

    assert after == before, "sources.py must never create or modify a vault note"
    assert (vault / ".mb" / "sources.json").is_file()
    assert (vault / ".mb" / "ingest-log.jsonl").is_file()
