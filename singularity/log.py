"""One logger, configured by whoever owns the process.

The engine had no notion of "telling the user something" other than `print`,
so domain modules printed straight to stdout/stderr. That works for exactly one
caller — a CLI in the foreground — and silently misbehaves for every other:
a daemon writes user-facing notices into its log file, a hook corrupts the JSON
its client is parsing, a test has to capture streams to assert on a message.

The split this module draws:

* **notices** — "something happened you should know about" (a config migrated,
  a daemon needs restarting). They belong to a logger: the entry point decides
  whether they are shown, where, and at which level.
* **reports** — the output a command exists to produce (`mb curator`'s
  findings, `mb status`'s snapshot). Those are DATA, returned to the caller and
  rendered by the presentation layer; they never come through here.

Handlers are attached only by an entry point (`configure_for_cli`). A library
import must not touch global logging state, so importing this module changes
nothing — the null handler keeps a stray notice from printing an
`No handlers could be found` warning in an embedding process.
"""
from __future__ import annotations

import logging
import sys

LOGGER_NAME = "mb"

log = logging.getLogger(LOGGER_NAME)
log.addHandler(logging.NullHandler())


def configure_for_cli(stream=None, level: int = logging.INFO) -> None:
    """Route notices to stderr, one line each, exactly as `print` used to.

    stderr rather than stdout on purpose: `mb search --json | jq` must keep
    getting only the payload, and a notice about a migrated config must not
    become part of it. Idempotent — a second call replaces the handler instead
    of doubling every line.
    """
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    for existing in list(log.handlers):
        if not isinstance(existing, logging.NullHandler):
            log.removeHandler(existing)
    log.addHandler(handler)
    log.setLevel(level)
    # propagate stays on: turning it off is global state that outlives the call
    # and hides every notice from a host application's own logging (and from
    # pytest's caplog). The root logger has no handler in a plain CLI run, so
    # nothing is duplicated.
