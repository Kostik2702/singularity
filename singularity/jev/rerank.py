"""Search rerank: Jev reads each candidate excerpt and says whether it answers.

Imitates `search/graph_boost.py`: it reorders what the engine returned and
never adds or removes a hit beyond the cut back to `top`. With Jev off, keyless,
failing or timing out, the answer is the engine's answer untouched.

Only wiki notes are sent. A note classified personal (frontmatter project, or a
path under `personal_roots`) is never sent and keeps its slot; code and raw
source hits are not notes and are not sent either. Every outgoing text passes
`redact()` first, and each candidate sits in its own question so none sees the
others - the vendor documents that irrelevant detail in the state costs accuracy.
"""
from __future__ import annotations

import math
import time

from ..knowledge.redact import redact
from ..search.chunking import infer_project
from . import client, enabled, log, noul_confidence

JEV_TIMEOUT = 3.5
EXCERPT_CHARS = 600
MIN_CANDIDATES = 15
MAX_CANDIDATES = 30
TASK = "Does this note excerpt contain information that answers the query in state?"
CRITERIA = {
    "true": "the excerpt answers or directly addresses the query",
    "false": "the excerpt is about something else or only shares words with the query",
}


def active(cfg, *, disabled: bool = False) -> str | None:
    """The key to rerank with, or None when this search stays as the engine made it."""
    if not enabled(cfg) or cfg.get("jev_search", True) is not True or disabled:
        return None
    return client.resolve_key()


def candidates_wanted(top: int) -> int:
    """Up to 30 are reranked; a larger `top` keeps its tail, so ask for all of it."""
    return max(top, min(MAX_CANDIDATES, max(top * 3, MIN_CANDIDATES)))


def is_personal(meta: dict, personal_roots: list[str]) -> bool:
    """Personal for an OUTBOUND call: the engine's classification (an explicit
    frontmatter project wins over a root) OR a path under any personal root.
    A note filed under a personal root does not become sendable by carrying a
    `project:` line; the engine's own classification is untouched elsewhere."""
    path = str(meta.get("file", ""))
    return "personal" in (infer_project({"project": meta.get("project")}, path, personal_roots),
                          infer_project({}, path, personal_roots))


def sendable(hit: dict, personal_roots: list[str]) -> bool:
    meta = hit.get("meta") or {}
    return hit.get("source") == "memory" and not is_personal(meta, personal_roots)


def build_request(query: str, hits: list[dict], personal_roots: list[str]) -> tuple[dict, dict, list[int]]:
    """(state, questions, positions): `questions["c<i>"]` describes `hits[positions[i]]`."""
    questions: dict = {}
    positions: list[int] = []
    for pos, hit in enumerate(hits):
        if not sendable(hit, personal_roots):
            continue
        meta = hit["meta"]
        questions[f"c{len(positions)}"] = {
            "type": "noul",
            "instructions": {"task": TASK, "note": {
                "title": redact(str(meta.get("title", ""))).text,
                "section": redact(str(meta.get("section", ""))).text,
                "excerpt": redact(str(meta.get("content", ""))[:EXCERPT_CHARS]).text}},
            "criteria": CRITERIA,
        }
        positions.append(pos)
    return {"query": redact(query).text}, questions, positions


def order(hits: list[dict], positions: list[int], probabilities: list[float]) -> list[dict]:
    """Sent hits sorted by probability (ties keep the engine's order) into the
    slots the sent hits held; every other hit stays where it was."""
    ranked = sorted(range(len(positions)), key=lambda i: -probabilities[i])
    out = list(hits)
    for slot, i in zip(positions, ranked, strict=True):
        out[slot] = hits[positions[i]]
    return out


def _rerank(cfg, query: str, hits: list[dict], explain: bool, key: str | None) -> list[dict] | None:
    """The reordered `hits`, or None when Jev did not (or could not) answer."""
    state, questions, positions = build_request(query, hits, cfg.get("personal_roots") or [])
    if not questions:
        return None
    try:
        reply = client.ask(state, questions, timeout=JEV_TIMEOUT,
                           deadline=time.monotonic() + JEV_TIMEOUT, key=key)
    except client.JevError as exc:
        log.append(cfg, "search", text=query, error=exc.tag)
        return None
    try:
        probs = [float(reply["answers"][q]["noul"]) for q in questions]
        if not all(math.isfinite(p) for p in probs):
            raise ValueError("non-finite probability")
    except (KeyError, TypeError, ValueError, AttributeError):
        log.append(cfg, "search", text=query, error="bad_json:null")
        return None
    for i, p in enumerate(probs):
        hit = hits[positions[i]]
        hit["jev"] = round(p, 4)
        if explain:
            hit.setdefault("explain", {})["jev"] = round(p, 4)
    log.append(cfg, "search", text=query, confidence=noul_confidence(max(probs)),
               cost=reply["cost"], ms=reply["ms"], model=reply["model"],
               request_id=reply["request_id"])
    return order(hits, positions, probs)


def apply(cfg, resp: dict, query: str, top: int, *, excerpt: int | None,
          explain: bool = False, key: str | None = None) -> dict:
    """Rerank a `format: json` answer in place and cut it back to `top`.

    Only the first MAX_CANDIDATES hits are reranked; a larger `top` keeps the
    rest after them, unchanged. `excerpt` is the excerpt length the caller would
    have shown without Jev (None = full content): the candidates were fetched
    longer, and the shown content is cut back so a reranked line reads like any
    other. Never raises: any failure leaves the engine's order."""
    results = list(resp.get("results") or [])
    hits, tail = results[:MAX_CANDIDATES], results[MAX_CANDIDATES:]
    try:
        reordered = _rerank(cfg, query, hits, explain, key)
    except Exception:  # noqa: BLE001 - a rerank is a courtesy, never a traceback
        log.append(cfg, "search", text=query, error="internal:null")
        reordered = None
    resp["reranked"] = reordered is not None
    hits = (reordered if reordered is not None else hits) + tail
    hits = hits[:top]
    if excerpt is not None:
        for hit in hits:
            meta = hit.get("meta") or {}
            content = meta.get("content")
            if isinstance(content, str) and len(content) > excerpt:
                meta["content"] = content[:excerpt] + "..."
    resp["results"] = hits
    return resp
