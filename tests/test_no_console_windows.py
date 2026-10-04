"""No console window per child process on Windows -- enforced.

A console program (git, schtasks, node...) started by a process that has no
console of its own is given a console window of its own. The scheduler's jobs
and the daemon run as pythonw, which has none, so on 2026-10-04 `nightly`
flashed a terminal ~380 times in the minutes after a boot: the code graph's
`git` calls, one window each. The flag does not help on the parent -- pythonw
is a GUI program -- so every call that starts a process passes
`creationflags=` itself: `platform.proc.NO_WINDOW`, or the same expression in
the factory, which imports nothing from the engine.

EXEMPT names the calls that go without it on purpose, with the reason. As in
test_architecture: no new offenders, and an entry that no longer matches a call
must be removed, so the list keeps describing the code.
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess

import pytest

from singularity.platform import proc

PKG = pathlib.Path(__file__).resolve().parents[1] / "singularity"
SPAWNERS = {"run", "Popen", "call", "check_call", "check_output"}
NO_FLAG_POSSIBLE = {"system", "popen"}             # os.system / os.popen: no creationflags at all

EXEMPT = {
    # `mb llm code` hands the terminal to an interactive Claude Code session:
    # it has to share the user's console, a hidden one would swallow it.
    ("llm", "code"): "interactive child, inherits the user's console",
    # POSIX-only code: there is no console window to create.
    ("platform.proc", "spawn_detached"): "POSIX branch; the Windows branch passes the flag",
    ("platform.scheduler", "_linux_register"): "crontab, POSIX only",
    ("platform.scheduler", "_linux_unregister"): "crontab, POSIX only",
}


def _is_spawn(call: ast.Call, from_subprocess: set[str]) -> bool:
    f = call.func
    if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
        if f.value.id == "subprocess" and f.attr in SPAWNERS:
            return True
        if f.value.id == "os" and f.attr in NO_FLAG_POSSIBLE:
            return True
    return isinstance(f, ast.Name) and f.id in from_subprocess


def _states_flags(call: ast.Call) -> bool:
    # `**kwargs` may or may not carry the flag; the call has to say it outright
    return any(k.arg == "creationflags" for k in call.keywords)


def unflagged_spawns(src: str, module: str) -> set[tuple[str, str]]:
    """(module, enclosing function) of every process start that does not pass
    `creationflags=`. Module-level code reports as `<module>`."""
    tree = ast.parse(src)
    from_subprocess = {a.asname or a.name for n in ast.walk(tree)
                       if isinstance(n, ast.ImportFrom) and n.module == "subprocess"
                       for a in n.names if a.name in SPAWNERS}
    found: set[tuple[str, str]] = set()

    def visit(node: ast.AST, func: str) -> None:
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, ast.Call) and _is_spawn(child, from_subprocess)
                    and not _states_flags(child)):
                found.add((module, func))
            inner = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            visit(child, inner)

    visit(tree, "<module>")
    return found


def _package_offenders() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for f in sorted(PKG.rglob("*.py")):
        if "__pycache__" in f.parts:
            continue
        module = ".".join(f.relative_to(PKG).with_suffix("").parts)
        out |= unflagged_spawns(f.read_text(encoding="utf-8", errors="replace"), module)
    return out


def test_every_process_start_says_it_opens_no_window():
    actual = _package_offenders()
    new = sorted(actual - EXEMPT.keys())
    assert not new, (
        f"these start a process without `creationflags=`: {new}. On Windows each such "
        "child of pythonw or of the daemon flashes a console window. Pass "
        "`creationflags=proc.NO_WINDOW`, or add the call to EXEMPT with a reason.")
    stale = sorted(EXEMPT.keys() - actual)
    assert not stale, (
        f"these EXEMPT entries no longer match an unflagged call -- remove them so the "
        f"list keeps describing the code: {stale}")


@pytest.mark.parametrize("src, expected", [
    ("import subprocess\ndef f():\n    subprocess.run(['git'], capture_output=True)\n",
     {("m", "f")}),
    ("import subprocess\ndef f():\n    subprocess.run(['git'], creationflags=0)\n", set()),
    ("from subprocess import Popen as P\nclass C:\n    def g(self):\n        P(['x'])\n",
     {("m", "g")}),
    ("import os\nos.system('ver')\n", {("m", "<module>")}),
    ("import subprocess\ndef f(**kw):\n    subprocess.check_output(['x'], **kw)\n",
     {("m", "f")}),
    ("import subprocess\ndef outer():\n    def inner():\n        subprocess.call(['x'])\n",
     {("m", "inner")}),
])
def test_the_scanner_can_actually_see_an_unflagged_call(src, expected):
    """A green guard over healthy code says nothing until it has gone red on a
    planted violation, in each form the code could take."""
    assert unflagged_spawns(src, "m") == expected


def test_no_window_is_the_windows_flag_and_zero_elsewhere():
    expected = 0x08000000 if os.name == "nt" else 0
    assert expected == proc.NO_WINDOW


def test_the_background_code_update_gets_a_hidden_console_not_none(monkeypatch, tmp_path):
    """`mb code update` started behind a query: DETACHED_PROCESS gave it no
    console at all, so its own children -- git, and the build's pool workers
    once a unit passes 2000 files -- each flashed a window (measured: two
    workers, two visible windows). A hidden console of its own is inherited
    by all of them; the new process group keeps it out of the terminal's Ctrl+C."""
    from singularity.codegraph import update

    seen: dict = {}

    def fake_popen(argv, **kw):
        seen.update(kw)

    monkeypatch.setattr(update, "_lock_alive", lambda cfg, key: False)
    monkeypatch.setattr(update.versions, "parser_available", lambda: True)
    monkeypatch.setattr(update.store, "unit_dir", lambda cfg, key: tmp_path)
    monkeypatch.setattr(update.subprocess, "Popen", fake_popen)
    monkeypatch.delenv("MB_CODE_NO_BACKGROUND", raising=False)

    assert update.spawn(None, "unit", tmp_path, "update") is True
    flags = seen["creationflags"]
    if os.name == "nt":
        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert not flags & subprocess.DETACHED_PROCESS
    else:
        assert flags == 0


@pytest.mark.skipif(os.name != "nt", reason="console windows exist only on Windows")
def test_a_child_of_pythonw_started_with_the_flag_has_no_window():
    """The mechanism itself, end to end: pythonw (no console, as the scheduler
    runs it) starts a console program with NO_WINDOW; the child must find no
    console window. Without the flag the same child reports a visible one."""
    pythonw = proc.python_executable(windowed=True)
    assert pythonw.lower().endswith("pythonw.exe"), f"no pythonw next to {pythonw}"
    child = pathlib.Path(pythonw).with_name("python.exe")
    probe = ("import ctypes; k = ctypes.windll.kernel32; u = ctypes.windll.user32; "
             "h = k.GetConsoleWindow(); print(bool(h and u.IsWindowVisible(h)))")
    parent = ("import subprocess, sys; from singularity.platform.proc import NO_WINDOW; "
              f"r = subprocess.run([{str(child)!r}, '-c', {probe!r}], capture_output=True, "
              "text=True, creationflags=NO_WINDOW); print(r.stdout.strip())")
    env = dict(os.environ, PYTHONPATH=str(PKG.parent))
    done = subprocess.run([pythonw, "-c", parent], capture_output=True, text=True,
                          timeout=60, env=env, cwd=str(PKG.parent), creationflags=proc.NO_WINDOW)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "False", f"the child saw a visible console window: {done.stdout!r}"
