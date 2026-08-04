"""Make the CLIs reachable by name (issue #15).

`pip install -e` generates the seven console scripts declared in
`[project.scripts]`, but into `<engine>/.venv/bin` (`Scripts` on Windows) —
a directory nothing ever puts on `PATH`. So a successful install left every
command the README documents (`mb doctor`, `mb search …`, `imgqa …`) failing
with `command not found` in a fresh shell.

Two mechanisms, because the platforms differ in kind:

- **POSIX** — symlink the console scripts into `~/.local/bin`, the
  conventional per-user bin directory. If that directory is not on `PATH`,
  print the exact line to add and let the user decide: silently editing
  someone's shell rc is not ours to do.
- **Windows** — there is no equivalent convention, so append the venv
  `Scripts` directory to the **user** `PATH` (`HKCU\\Environment`) through the
  registry. `setx` is not used: it truncates at 1024 characters and rewrites
  `REG_EXPAND_SZ` values as literal `REG_SZ`, which corrupts a `PATH`
  containing `%USERPROFILE%`.

Ownership rule, the same one skills and hooks already follow: we only ever
remove what resolves back into THIS engine. A `~/.local/bin/mb` belonging to
another clone, or a `PATH` entry we did not add, is reported and left alone.

Why the console scripts and not `bin/`: a console script sets `sys.path[0]` to
its own directory, so it is immune to the CWD shadowing of issue #16, and pip
keeps it in sync with `[project.scripts]`.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]

# Fallback for a tree without pyproject.toml (an installed wheel). The parsed
# value wins whenever the file is there, so this list cannot drift silently.
FALLBACK_TOOLS = ["mb", "imgqa", "design-scout", "pattern-scout",
                  "component-intake", "golden", "factory"]

POSIX_LINK_DIR = Path.home() / ".local" / "bin"


def console_tools() -> list[str]:
    """Tool names from `[project.scripts]` — the single source of truth."""
    pyproject = ENGINE / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return list(FALLBACK_TOOLS)
    m = re.search(r"^\[project\.scripts\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return list(FALLBACK_TOOLS)
    names = re.findall(r"^\s*([A-Za-z0-9_.\-]+)\s*=", m.group(1), re.M)
    return names or list(FALLBACK_TOOLS)


def venv_bin_dir() -> Path | None:
    """Where this engine's venv keeps its console scripts, if it has one."""
    for rel in (".venv/bin", ".venv/Scripts"):
        d = ENGINE / rel
        if d.is_dir():
            return d
    return None


def _script_path(bin_dir: Path, tool: str) -> Path | None:
    for candidate in (bin_dir / tool, bin_dir / f"{tool}.exe"):
        if candidate.exists():
            return candidate
    return None


def _resolves_into_engine(p: Path) -> bool:
    try:
        return ENGINE.resolve() in p.resolve().parents
    except OSError:
        return False


# ---------------------------------------------------------------- POSIX ----

def _posix_register(bin_dir: Path) -> dict:
    POSIX_LINK_DIR.mkdir(parents=True, exist_ok=True)
    linked, ok, foreign, missing = [], [], [], []
    for tool in console_tools():
        src = _script_path(bin_dir, tool)
        if src is None:
            missing.append(tool)
            continue
        dst = POSIX_LINK_DIR / tool
        if dst.is_symlink() or dst.exists():
            try:
                if dst.resolve() == src.resolve():
                    ok.append(tool)
                    continue
            except OSError:
                pass
            if not (dst.is_symlink() and _resolves_into_engine(dst)):
                foreign.append(tool)         # someone else's file: never clobber
                continue
            dst.unlink()                     # our own link, stale target
        try:
            dst.symlink_to(src)
            linked.append(tool)
        except OSError:
            shutil.copy2(src, dst)           # no symlink privilege: copy
            linked.append(tool)
    return {"dir": str(POSIX_LINK_DIR), "linked": linked, "ok": ok,
            "foreign": foreign, "missing": missing}


def _posix_uninstall() -> dict:
    removed, kept = [], []
    for tool in console_tools():
        dst = POSIX_LINK_DIR / tool
        if not (dst.is_symlink() or dst.exists()):
            continue
        if dst.is_symlink() and _resolves_into_engine(dst):
            dst.unlink()
            removed.append(tool)
        else:
            kept.append(tool)                # foreign or another clone's
    return {"removed": removed, "kept": kept}


# -------------------------------------------------------------- Windows ----

_WIN_ENV_KEY = r"Environment"


def _win_read_path() -> tuple[str, int]:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_ENV_KEY) as key:
        try:
            value, vtype = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            return "", winreg.REG_EXPAND_SZ
    return value, vtype


def _win_write_path(value: str, vtype: int) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_ENV_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, vtype, value)
    _win_broadcast()


def _win_broadcast() -> None:
    """Tell running shells the environment changed. Best-effort: a new terminal
    picks the value up regardless, this only spares the user one restart."""
    try:
        import ctypes

        HWND_BROADCAST, WM_SETTINGCHANGE, SMTO_ABORTIFHUNG = 0xFFFF, 0x1A, 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, ctypes.c_wchar_p("Environment"),
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(ctypes.c_ulong()))
    except Exception:                        # noqa: BLE001 — cosmetic only
        pass


def _win_entries(value: str) -> list[str]:
    return [p for p in value.split(os.pathsep) if p.strip()]


def _same_dir(a: str, b: Path) -> bool:
    try:
        return Path(os.path.expandvars(a)).resolve() == b.resolve()
    except OSError:
        return False


def _win_register(bin_dir: Path) -> dict:
    value, vtype = _win_read_path()
    entries = _win_entries(value)
    if any(_same_dir(e, bin_dir) for e in entries):
        return {"dir": str(bin_dir), "path_entry": "already present"}
    new = os.pathsep.join([*entries, str(bin_dir)])
    _win_write_path(new, vtype)
    return {"dir": str(bin_dir), "path_entry": "added"}


def _win_uninstall(bin_dir: Path) -> dict:
    value, vtype = _win_read_path()
    entries = _win_entries(value)
    keep = [e for e in entries if not _same_dir(e, bin_dir)]
    if len(keep) == len(entries):
        return {"path_entry": "not present"}
    _win_write_path(os.pathsep.join(keep), vtype)
    return {"path_entry": "removed"}


# --------------------------------------------------------------- public ----

def resolved_mb() -> str | None:
    """Where `mb` resolves for a process with this PATH, if anywhere."""
    found = shutil.which("mb")
    return str(Path(found).resolve()) if found else None


def status() -> dict:
    """What doctor's D7 needs: is `mb` reachable, and is it OUR `mb`?"""
    bin_dir = venv_bin_dir()
    found = resolved_mb()
    return {
        "engine": str(ENGINE),
        "venv_bin": str(bin_dir) if bin_dir else None,
        "resolved": found,
        "on_path": found is not None,
        "is_ours": bool(found and _resolves_into_engine(Path(found))),
    }


def _shell_hint(directory: Path) -> str:
    shell = Path(os.environ.get("SHELL", "")).name or "sh"
    rc = {"zsh": "~/.zshrc", "bash": "~/.bashrc",
          "fish": "~/.config/fish/config.fish"}.get(shell, "your shell rc file")
    if shell == "fish":
        return f'fish_add_path "{directory}"      # or add to {rc}'
    return f'export PATH="{directory}:$PATH"      # add to {rc}'


def main() -> int:
    bin_dir = venv_bin_dir()
    if bin_dir is None:
        print("[path] no .venv in the engine — nothing to register "
              "(run the installer, not this file alone)", file=sys.stderr)
        return 1

    if os.name == "nt":
        res = _win_register(bin_dir)
        print(f"[path] user PATH entry {res['path_entry']}: {res['dir']}")
        if res["path_entry"] == "added":
            print("[path] open a NEW terminal for it to take effect")
    else:
        res = _posix_register(bin_dir)
        print(f"[path] {res['dir']}: {len(res['linked'])} linked, "
              f"{len(res['ok'])} already ok"
              + (f", {len(res['foreign'])} left alone (not ours)" if res["foreign"] else "")
              + (f", {len(res['missing'])} not built" if res["missing"] else ""))
        for tool in res["foreign"]:
            print(f"[path] {POSIX_LINK_DIR / tool} is not ours — left untouched; "
                  f"remove it yourself if you want this engine's {tool}",
                  file=sys.stderr)
        if res["missing"]:
            print(f"[path] not generated by pip: {', '.join(res['missing'])} — "
                  f"re-run `pip install -e .` in the engine", file=sys.stderr)
        entries = [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]
        if not any(_same_dir(str(p), POSIX_LINK_DIR) for p in entries):
            print(f"[path] {POSIX_LINK_DIR} is NOT on your PATH. Add it:\n"
                  f"       {_shell_hint(POSIX_LINK_DIR)}")

    st = status()
    if st["on_path"] and not st["is_ours"]:
        print(f"[path] warning: `mb` currently resolves to {st['resolved']}, "
              f"which is outside {ENGINE} — another clone is shadowing this one",
              file=sys.stderr)
    return 0


def uninstall() -> dict:
    """Remove only what we own — the engine-pointing links / our PATH entry."""
    bin_dir = venv_bin_dir()
    if os.name == "nt":
        if bin_dir is None:
            return {"path_entry": "no venv"}
        return _win_uninstall(bin_dir)
    return _posix_uninstall()


if __name__ == "__main__":
    sys.exit(main())
