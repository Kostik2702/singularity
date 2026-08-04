"""Find a Node-based CLI that `PATH` alone cannot see (issue #10).

`shutil.which` answers a narrower question than the one the registrars ask.
nvm is a shell function sourced from `~/.zshrc`, not a binary on the system
path, so a `gemini` installed with `npm i -g` under nvm is invisible to any
non-interactive process: an installer run via `sh install/install.sh`, a CI
job, a launchd/systemd job, anything under `sudo`, an IDE terminal that skips
the interactive rc file. nvm is the mainstream way to install Node on macOS
and both `@google/gemini-cli` and the Grok CLI ship via npm, so this is the
common case, not an edge one.

The observed damage: `~/.gemini` exists so the registrar proceeds, but
`shutil.which("gemini")` returns None, so the version is unknown, so hooks are
skipped — while the install exits 0 and the message blames a version
constraint (`requires gemini >= 0.26.0`) that the installed 0.53.1 satisfies.

So: look where version managers actually put things before concluding a CLI is
unreachable. Read-only, no shell spawned, no rc file sourced.
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

__all__ = ["find_cli", "search_locations"]


def _node_version_dirs(root: Path) -> list[Path]:
    """`<root>/<version>/bin` newest first — nvm keeps one tree per Node."""
    if not root.is_dir():
        return []

    def key(p: Path) -> tuple:
        m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", p.name)
        return tuple(int(g) for g in m.groups()) if m else (0, 0, 0)

    try:
        versions = sorted((p for p in root.iterdir() if p.is_dir()), key=key, reverse=True)
    except OSError:
        return []
    return [v / "bin" for v in versions]


def search_locations() -> list[Path]:
    """Directories to try after `PATH`, most authoritative first.

    Kept as data so the failure message can name exactly what was searched —
    "not found" is only useful when the user can see where we looked."""
    home = Path.home()
    dirs: list[Path] = []

    # nvm: $NVM_BIN is exported inside a shell where nvm was sourced; the
    # versions tree is there regardless.
    nvm_bin = os.environ.get("NVM_BIN", "").strip()
    if nvm_bin:
        dirs.append(Path(nvm_bin))
    nvm_dir = os.environ.get("NVM_DIR", "").strip()
    dirs += _node_version_dirs(Path(nvm_dir or home / ".nvm") / "versions" / "node")

    # fnm — the multishell dir is the active one, then the version store.
    fnm_multishell = os.environ.get("FNM_MULTISHELL_PATH", "").strip()
    if fnm_multishell:
        dirs.append(Path(fnm_multishell) / "bin")
    for base in (home / ".local" / "share" / "fnm", home / ".fnm"):
        for d in _node_version_dirs(base / "node-versions"):
            dirs.append(d.parent / "installation" / "bin")

    dirs += [
        home / ".volta" / "bin",                      # volta
        home / ".asdf" / "shims",                     # asdf
        home / ".bun" / "bin",                        # bun
        home / ".local" / "bin",
        Path(os.environ.get("APPDATA", "")) / "npm" if os.name == "nt" else None,
        Path("/opt/homebrew/bin"),                    # Apple silicon brew
        Path("/usr/local/bin"),
        home / "node_modules" / ".bin",
    ]
    return [d for d in dirs if d is not None and str(d) not in ("", ".")]


def _executable_in(directory: Path, name: str) -> str | None:
    """`name` (with any PATHEXT suffix) inside one directory.

    Deliberately not `shutil.which(name, path=…)`: half the test suite stubs
    `shutil.which` with a one-argument lambda to mean "nothing installed", and
    a two-argument call there would blow up rather than answer."""
    suffixes = [""]
    if os.name == "nt":
        suffixes += [e.lower() for e in
                     os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep)
                     if e.strip()]
    for suffix in suffixes:
        candidate = directory / f"{name}{suffix}"
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def find_cli(name: str) -> tuple[str | None, str | None]:
    """Locate `name`. Returns (absolute path, where it was found).

    `where` is "PATH" or the directory that supplied it, so callers can say
    something more useful than "not found" — and so a machine whose CLI is only
    reachable through a version manager becomes a diagnosable state instead of
    an invisible one."""
    found = shutil.which(name)
    if found:
        return found, "PATH"
    for d in search_locations():
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        hit = _executable_in(d, name)
        if hit:
            return hit, str(d)
    return None, None
