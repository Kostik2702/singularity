"""Jev (TypeSafe's decision API) as a second opinion for the lead session.

Jev decides and never writes: it answers a yes/no, a choice or a score about a
text, and the engine turns that into a hint. Nothing here edits a tool call.
`client` is the only module that talks HTTP; `log` keeps the journal every
other part appends to; `router`, `pick` and `rerank` are the three consumers.

Off unless `jev_enabled` is true. Below `THRESHOLD` confidence Jev does not
decide - the caller does, as the vendor documents.
"""
from __future__ import annotations

THRESHOLD = 0.6


def enabled(cfg) -> bool:
    return cfg.get("jev_enabled") is True


def noul_confidence(p: float) -> float:
    """A noul answer has no confidence of its own; the owner's spec reads it as
    how far the probability sits from a coin flip, on the winning side."""
    return max(p, 1.0 - p)
