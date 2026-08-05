"""How often each note was actually opened — the signal age cannot give.

`mb curator` can already say a note is old. Age is a poor proxy for value in a
knowledge vault: a decision from two years ago may be permanently correct, while
a reference written last month may never have been needed once. What separates
them is whether anyone ever went back to it.

The signal is free. `mb reflect` already walks every transcript offline and
already resolves which reads landed inside the vault — it counted them and threw
the paths away. This module keeps them.

Two deliberate limits, both about not overclaiming:

* **Accumulating, not recomputed.** Transcripts are pruned by their clients, so
  a full recount would silently reset a note's history to zero the moment its
  session aged out. Counts are merged forward, with `scanned_through` marking
  how far the last walk reached so the next one does not count the same session
  twice. A session appended to across two runs can double-count part of itself;
  for a tie-break and a staleness report that is noise, not a defect, and it is
  said here rather than discovered later.
* **Not a popularity ranking.** A note read once at the exact right moment is
  worth more than one skimmed twenty times. These numbers break ties and feed a
  report; they are not a score.

Privacy: vault-relative note paths and integers. Nothing about a session — no
id, no transcript path, no query, no content. The paths are already in the
search index, so this file discloses nothing the vault did not already hold.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any

from ..config import Config, atomic_write_json

USAGE_FILE = "usage.json"
SCHEMA = 1


def path(cfg: Config):
    return cfg.mb_dir / USAGE_FILE


def load(cfg: Config) -> dict[str, Any]:
    """Never raises: a corrupt or absent file means "nothing known yet", which
    is the same thing a fresh install means, and both must degrade to a
    tie-break that does nothing rather than to an error in the search path."""
    try:
        data = json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "scanned_through": 0.0, "notes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("notes"), dict):
        return {"schema": SCHEMA, "scanned_through": 0.0, "notes": {}}
    return data


def reads_by_note(cfg: Config) -> dict[str, int]:
    """{vault-relative path: times opened}. The shape the ranking wants."""
    notes = load(cfg).get("notes") or {}
    out: dict[str, int] = {}
    for rel, entry in notes.items():
        if isinstance(entry, dict) and isinstance(entry.get("reads"), int):
            out[rel] = entry["reads"]
    return out


def update(cfg: Config, *, now: float | None = None) -> dict[str, Any]:
    """Walk transcripts newer than the last run and merge their note reads in.

    Returns a summary; writes `.mb/usage.json`. Safe to call when no client
    transcripts exist — the answer is simply zero new sessions.
    """
    from . import reflect as reflect_mod

    state = load(cfg)
    since = float(state.get("scanned_through") or 0.0)
    stamp = now if now is not None else time.time()

    sessions = reflect_mod.walk_sessions(cfg, since)
    merged: dict[str, dict[str, Any]] = dict(state.get("notes") or {})
    today = datetime.fromtimestamp(stamp).date().isoformat()
    new_reads = 0
    for session in sessions:
        for rel, count in session.note_reads.items():
            entry = merged.setdefault(rel, {"reads": 0, "last": today})
            entry["reads"] = int(entry.get("reads") or 0) + int(count)
            entry["last"] = today
            new_reads += count

    out = {"schema": SCHEMA, "scanned_through": stamp, "notes": merged}
    atomic_write_json(path(cfg), out)
    return {"sessions": len(sessions), "new_reads": new_reads,
            "notes_tracked": len(merged)}


def never_opened(cfg: Config, known_notes: list[str]) -> list[str]:
    """Notes the vault holds that no recorded session ever opened.

    `known_notes` comes from the caller (the index), so this module never scans
    the vault itself and cannot disagree with what search can actually see.
    """
    seen = set(reads_by_note(cfg))
    return sorted(rel for rel in known_notes if rel not in seen)
