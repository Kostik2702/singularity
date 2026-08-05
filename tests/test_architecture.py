"""Layer boundaries, enforced — and the debt against them, counted.

The engine grew to 23k lines as one flat namespace, so "the domain must not
print" is currently false: 13 modules do. Asserting the ideal would mean a red
suite and a rule everyone learns to skip, so each invariant below carries an
explicit EXEMPT list — the debt, written down where it is checked.

Two properties make that honest rather than decorative:

* the lists may only SHRINK — a module that starts printing, exiting, or
  parsing arguments outside its layer fails the suite;
* an exemption that gets fixed must be REMOVED — a stale entry fails too, so
  the list cannot quietly outlive the problem it describes.

Presentation layer = the places whose job is to talk to a human or a harness:
`cli`/`facade`/`render`, the per-area command handlers in `commands/*`, the
seven sibling CLIs (`*.cli`), and `hooks/*` (they print the JSON their client
reads).
"""
from __future__ import annotations

import ast
import pathlib

import pytest

PKG = pathlib.Path(__file__).resolve().parents[1] / "singularity"


def _modules() -> dict[str, str]:
    out = {}
    for f in sorted(PKG.rglob("*.py")):
        if "__pycache__" in str(f) or "/skills/" in str(f):
            continue
        out[".".join(f.relative_to(PKG).with_suffix("").parts)] = f.read_text(
            encoding="utf-8", errors="replace")
    return out


def _is_presentation(module: str) -> bool:
    return (module in {"cli", "facade", "render", "__main__"}
            or module.startswith("commands.")      # `mb <command>` handlers
            or module.endswith(".cli") or module.startswith("hooks."))


def _calls(src: str, func: str) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:                                   # pragma: no cover
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == func:
            return True
        if isinstance(f, ast.Attribute) and f.attr == func.split(".")[-1] and "." in func:
            owner = func.split(".")[0]
            if isinstance(f.value, ast.Name) and f.value.id == owner:
                return True
    return False


def _imports_module(src: str, target: str) -> bool:
    try:
        tree = ast.parse(src)
    except SyntaxError:                                   # pragma: no cover
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == target or mod == f"singularity.{target}":
                return True
            if node.level and mod == "" and any(a.name == target for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name in (target, f"singularity.{target}") for a in node.names):
                return True
    return False


def _check(actual: set[str], exempt: set[str], what: str):
    """Same assertion for every invariant: no new offenders, no stale exemptions."""
    new = sorted(actual - exempt)
    assert not new, (
        f"{what} outside the presentation layer: {new}. "
        "Move the behaviour, or add the module to the EXEMPT list with a reason.")
    fixed = sorted(exempt - actual)
    assert not fixed, (
        f"these modules no longer {what} — remove them from the EXEMPT list "
        f"so it keeps describing reality: {fixed}")


# --- printing ---------------------------------------------------------------
# Debt: rendering lives inside domain modules, which is why each of them also
# carries a `--json` branch duplicating its own text output. Shrinks as the
# render layer takes over (plan step 1.3).
PRINT_EXEMPT = {
    # Reports: these print the answer their command was asked for. Splitting
    # collection from rendering is the remaining work (plan step 1.3b) —
    # `status.snapshot()` and `curator.analyze()` already return the data.
    "knowledge.contradictions", "machine.selftest",
    "doctor",           # a report generator by nature; kept deliberately
    "llm",              # `llm status|list|add|setup` output, notices already moved
    # Presentation for their own package, just not named `*.cli`:
    "reel.util", "imgqa._contract", "scout._contract",
}

# Empty, and it stays that way: `config` raised `ConfigError` instead of
# exiting in v0.10.1 — the change that lets `search/daemon.py` stop carrying its
# own copy of the config validation.
EXIT_EXEMPT: set[str] = set()

# Empty on purpose, and it stays that way: `llm` carried a dead `import
# argparse` (the parser had already moved to `cli.py`); the lint pass removed
# it and this invariant now pins the property instead of the intention.
ARGPARSE_EXEMPT: set[str] = set()


def test_only_presentation_prints():
    offenders = {m for m, src in _modules().items()
                 if not _is_presentation(m) and _calls(src, "print")}
    _check(offenders, PRINT_EXEMPT, "print")


def test_only_presentation_exits_the_process():
    offenders = {m for m, src in _modules().items()
                 if not _is_presentation(m) and _calls(src, "sys.exit")}
    _check(offenders, EXIT_EXEMPT, "call sys.exit")


def test_only_presentation_parses_arguments():
    offenders = {m for m, src in _modules().items()
                 if not _is_presentation(m) and _imports_module(src, "argparse")}
    _check(offenders, ARGPARSE_EXEMPT, "import argparse")


# --- direction of dependency ------------------------------------------------

def test_domain_never_imports_the_cli():
    """The CLI may know the domain; the reverse makes the domain unusable from
    a daemon, a hook or a test without dragging argparse in. `_contract` modules
    in imgqa/scout import their own package's cli — a real cycle, listed as debt
    and untangled in step 3.2."""
    offenders = {m for m, src in _modules().items()
                 if m not in {"cli", "facade", "__main__"} and not m.endswith(".cli")
                 and (_imports_module(src, "cli") or _imports_module(src, "facade"))}
    _check(offenders, {"imgqa._contract", "scout._contract"}, "import the cli layer")


def test_only_config_knows_the_vault_layout():
    """Where a thing lives inside the vault is `config`'s knowledge alone.

    Thirteen call sites built vault paths by hand, and `cfg.vault / rel`
    existed in three modules — one of them normalising Windows separators and
    two not, so a note recorded as `howto\\x.md` resolved to a file that did
    not exist on macOS. Named accessors (`resolve`, `memory_folder`,
    `templates_dir`, …) mean a layout change is one edit, not a search.
    """
    offenders = sorted(m for m, src in _modules().items()
                       if m != "config" and ("cfg.vault /" in src or "self.vault /" in src))
    assert not offenders, (
        f"these build vault paths by hand instead of asking config: {offenders}")


def test_the_debt_is_visible_and_finite():
    """One number to watch across the refactor; it must never grow."""
    total = len(PRINT_EXEMPT) + len(EXIT_EXEMPT) + len(ARGPARSE_EXEMPT)
    assert total <= 8, (
        f"layer debt grew to {total} — new exemptions need a decision, not a default")


@pytest.mark.parametrize("module", sorted(PRINT_EXEMPT | EXIT_EXEMPT | ARGPARSE_EXEMPT))
def test_every_exempt_module_exists(module):
    """A rename must not silently empty an invariant."""
    assert module in _modules(), f"EXEMPT lists a module that no longer exists: {module}"
