#!/usr/bin/env python3
"""Fallback launcher for the bin/ shims — never `python -m` (issue #16).

`python -m singularity` puts the CURRENT WORKING DIRECTORY first on
`sys.path`. One level above a standard clone the CWD contains a directory
named `singularity`, Python adopts it as a namespace package, and it shadows
the installed one:

    $ cd ~/projects && sh singularity/bin/mb --version
    ImportError: cannot import name '__version__' from 'singularity'
        (unknown location)          <- the namespace-package signature

`~/projects` is not an exotic place to stand: it is the natural parent of the
clone and a common CWD for agent sessions.

Running a *file* instead puts that file's own directory (`<engine>/bin`) on
`sys.path[0]`, and `<engine>/bin` contains no `singularity/`, so the shadowing
cannot happen. `PYTHONSAFEPATH=1` / `-P` would also work but only from Python
3.11, while `requires-python` is `>=3.10`.

This file is only the fallback: the shims prefer the generated console script
(`<engine>/.venv/bin/<tool>`), which pip keeps in sync with
`[project.scripts]` and which is immune for the same reason.

Usage: _launch.py <tool-name> <module>:<function> [args...]
"""
from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 2 or ":" not in argv[1]:
        print("usage: _launch.py <tool-name> <module>:<function> [args...]",
              file=sys.stderr)
        return 2
    tool, target = argv[0], argv[1]
    mod_name, _, func_name = target.partition(":")

    # Last resort for a checkout whose venv has no singularity installed
    # (a half-finished `pip install -e`): make the engine importable, but
    # APPEND — an installed package must always win, and the CWD must never
    # get a say.
    engine = str(Path(__file__).resolve().parents[1])
    if engine not in sys.path:
        sys.path.append(engine)

    # argparse reads sys.argv[1:] and prints sys.argv[0] as the program name;
    # give it the tool name rather than "_launch.py".
    sys.argv = [tool, *argv[2:]]
    rc = getattr(import_module(mod_name), func_name)()
    return 0 if rc is None else int(rc)


if __name__ == "__main__":
    sys.exit(main())
