"""Contradiction CANDIDATES (WIKI-LOOP W07) — stage 1, deterministic.

A vault that quietly holds two mutually exclusive statements is worse than an
empty one: it answers confidently and wrongly. `curator.lint()` already finds
near-duplicates (cosine > 0.90) and supersession by age gap — neither of which
is a contradiction. This module fills that hole with the only part a
deterministic engine can honestly own: *evidence*.

**These are candidates, not verdicts.** Precision without an LLM is low by
construction — "X is always on" and "X is never on" look nearly identical to an
embedding model, and so do "X is on for A" / "X is off for B", which do not
contradict at all. Adjudication belongs to a human or the `memory-curator`
skill (W09); this module's contract is that every candidate arrives with the
tokens that triggered it, so the judgement is checkable in seconds instead of
requiring a re-read of both notes.

Two ideas make it usable rather than noisy:

* **Similarity BAND, not threshold.** Below 0.72 the chunks are different
  topics; above 0.90 `curator.lint()` already reports them as near-duplicates.
  A contradiction lives in between: same subject, different claim.
* **Lexical divergence signals.** Five stdlib-only RU+EN checks, each of which
  must produce concrete evidence tokens. A pair with high cosine and NO signal
  is not reported at all — a "contradiction" nobody can point at is noise.

Report-only (D5): no function here writes to a note. The verdict journal
(`.mb/lint.json`) is engine state under `.mb/`, the same category as
`.mb/digest.json`.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np

from .config import Config, atomic_write_json

# Below the floor: different topics. Above the ceiling: curator.lint() already
# calls it a near-duplicate (curator.DUPLICATE_THRESHOLD = 0.90).
CONTRADICTION_BAND = (0.72, 0.90)
DEFAULT_LIMIT = 15
JOURNAL_NAME = "lint.json"
JOURNAL_SCHEMA_VERSION = 1
VERDICTS = ("false_positive", "resolved", "accepted")
# Bound on how many in-band pairs are scored. A vault of ~3k chunks yields a
# few hundred; the cap only exists so a 10x larger vault degrades into
# "top N by cosine" instead of into a stall. Exceeding it is REPORTED, never
# silent -- a truncated scan that looks complete is how "no contradictions"
# becomes a lie.
CANDIDATE_PAIR_CAP = 100_000
# `digest.py:36` is a code citation, not a claim about a value. Without this
# the real vault's most-linked design docs contradicted each other on every
# file:line they quoted.
_FILE_CITATION_RE = re.compile(r"^[\w\-]+\.[a-z]{1,4}$")

SIGNAL_WEIGHTS = {
    "boolean_flip": 1.0,       # `x: true` vs `x: false` -- as explicit as it gets
    "polarity_flip": 0.9,      # always/never, enable/disable on the same subject
    "negation_flip": 0.9,      # the SAME sentence, one side negated
    "numeric_mismatch": 0.6,   # same key, different value
    "supersede_marker": 0.5,   # one side declares itself outdated
    "negation_asymmetry": 0.4, # weakest: negation somewhere on one side only
}

# Minimum word-set overlap (Jaccard) at which a one-sided negation stops being
# incidental and becomes a flip of the same claim: "хук блокирует запись" vs
# "хук НЕ блокирует запись". Below it, the negation belongs to some other
# sentence of a long chunk and means nothing.
NEGATION_OVERLAP_MIN = 0.6

# A candidate needs a signal that asserts something, not merely one that
# NOTICES a difference. Measured on the real vault (3083 chunks): allowing any
# signal to trigger produced 8762 "candidates" out of 24068 in-band pairs —
# a report at that density is never read, which is risk R3 realized. Negation
# asymmetry alone fires on almost any long-vs-short pair, so it survives only
# as a weight modifier on top of a real trigger.
STRONG_SIGNALS = frozenset({"polarity_flip", "boolean_flip", "negation_flip",
                            "supersede_marker"})
TRIGGER_SIGNALS = STRONG_SIGNALS | {"numeric_mismatch"}

_WORD_BOUND = r"(?<![\w-])"

# --- signal vocabularies (RU + EN) -----------------------------------------
_NEGATION_MARKERS = (
    "не", "нет", "нельзя", "никогда", "без", "запрещено", "запрещён", "запрещен",
    "не следует", "not", "never", "no longer", "cannot", "can't", "don't",
    "must not", "should not", "disable", "disabled", "forbidden",
)
_POLARITY_PAIRS = (
    ("всегда", "никогда"),
    ("включить", "выключить"),
    ("включён", "выключен"),
    ("включен", "выключен"),
    ("разрешено", "запрещено"),
    ("обязательно", "нельзя"),
    ("нужно", "не нужно"),
    ("always", "never"),
    ("enable", "disable"),
    ("enabled", "disabled"),
    ("must", "must not"),
    ("should", "should not"),
    ("allowed", "forbidden"),
    ("required", "optional"),
)
_SUPERSEDE_MARKERS = (
    "deprecated", "superseded", "obsolete", "устарел", "устарело", "устаревший",
    "заменено", "заменён", "заменен", "выведен", "выведено из эксплуатации",
    "no longer used", "legacy",
)

_BOOL_RE = re.compile(r"([A-Za-zА-Яа-яЁё_][\w.\-]*)\s*[:=]\s*(true|false|True|False|да|нет)"
                      r"(?![\w-])")
_NUM_RE = re.compile(
    r"([A-Za-zА-Яа-яЁё_][\w.\-]{2,})"          # key, >= 3 chars
    r"(\s*[:=]\s*|\s+)"                        # separator (kept: it is a signal)
    r"[<>≥≤]?"
    r"(\d+(?:[.,]\d+)?)"                       # value
    r"([%A-Za-zА-Яа-яЁё]{0,6})")               # unit, ADJACENT only
# Two hard-won details, both from the first real-vault run (2026-07-28):
#   * the unit must TOUCH the number — `60d` is a unit, the `для` in `3.11 для`
#     is the next word, and allowing whitespace made every key read
#     `python=3.11для`;
#   * a bare integer with neither unit nor `:`/`=` is almost always an
#     enumeration artifact (`step 1`, `w0 3`, `mb-1 5`) rather than a claim.
#     Requiring a decimal, a unit, or an explicit separator removed the single
#     largest false-positive class.


def _norm(text: str) -> str:
    return " " + re.sub(r"\s+", " ", str(text or "").lower()) + " "


def _contains(haystack: str, needle: str) -> bool:
    return re.search(_WORD_BOUND + re.escape(needle) + r"(?![\w-])", haystack) is not None


# --- individual signals -----------------------------------------------------

_POLARITY_TERMS = tuple(sorted({term for pair in _POLARITY_PAIRS for term in pair}))
_TOKEN_RE = re.compile(r"[\w\-]{3,}")


def features(text: str) -> dict[str, Any]:
    """Everything the five signals need, extracted ONCE per chunk.

    Pairwise re-scanning was measured at 27.5s over 24k in-band pairs on the
    real vault; extracting per chunk instead turns O(pairs) regex work into
    O(chunks)."""
    norm = _norm(text)
    numbers: dict[tuple[str, str], set[str]] = {}
    for match in _NUM_RE.finditer(norm):
        key, sep, value, unit = (match.group(1).lower(), match.group(2),
                                 match.group(3).replace(",", "."), match.group(4).lower())
        explicit = (":" in sep) or ("=" in sep)
        if not (unit or "." in value or explicit):
            continue        # bare integer after a space -> enumeration, not a claim
        if _FILE_CITATION_RE.match(key):
            continue        # `digest.py: 36` -> a file:line pointer
        if "." in value:    # 0.20 and 0.2 are the same number, not a disagreement
            value = value.rstrip("0").rstrip(".") or "0"
        numbers.setdefault((key, unit), set()).add(value)

    booleans: dict[str, set[bool]] = {}
    for match in _BOOL_RE.finditer(norm):
        booleans.setdefault(match.group(1).lower(), set()).add(
            match.group(2).lower() in {"true", "да"})

    return {
        "numbers": numbers,
        "booleans": booleans,
        "negations": {m for m in _NEGATION_MARKERS if _contains(norm, m)},
        "polarity": {t for t in _POLARITY_TERMS if _contains(norm, t)},
        "supersede": {m for m in _SUPERSEDE_MARKERS if m in norm},
        # words of 3+ chars: short function words (and the Russian "не" itself)
        # are excluded so the overlap measures "is the REST of the text the same"
        "tokens": frozenset(_TOKEN_RE.findall(norm)),
    }


def _overlap(fa: dict[str, Any], fb: dict[str, Any]) -> float:
    a, b = fa["tokens"], fb["tokens"]
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _evidence_from_features(fa: dict[str, Any], fb: dict[str, Any]
                            ) -> dict[str, list[str]]:
    evidence: dict[str, list[str]] = {}

    numeric: list[str] = []
    for key in sorted(set(fa["numbers"]) & set(fb["numbers"])):
        left, right = fa["numbers"][key], fb["numbers"][key]
        if left == right:
            continue
        word, unit = key
        numeric += [f"{word}={v}{unit}" for v in sorted(left - right)]
        numeric += [f"{word}={v}{unit}" for v in sorted(right - left)]
    if numeric:
        evidence["numeric_mismatch"] = numeric

    if bool(fa["negations"]) != bool(fb["negations"]):
        markers = sorted(fa["negations"] ^ fb["negations"])
        overlap = _overlap(fa, fb)
        if overlap >= NEGATION_OVERLAP_MIN:
            # same words, one side negated -> a genuine flip of the same claim
            evidence["negation_flip"] = markers + [f"word_overlap={overlap:.2f}"]
        else:
            evidence["negation_asymmetry"] = markers

    polarity = sorted({f"{left}<->{right}" for left, right in _POLARITY_PAIRS
                       if (left in fa["polarity"] and right in fb["polarity"])
                       or (left in fb["polarity"] and right in fa["polarity"])})
    if polarity:
        evidence["polarity_flip"] = polarity

    # DISJOINT, not merely different: a chunk that shows both `x: false` and
    # `x: true` is documenting a before/after, and comparing it against either
    # side is not a contradiction.
    booleans = [f"{key}: {sorted(fa['booleans'][key])} vs {sorted(fb['booleans'][key])}"
                for key in sorted(set(fa["booleans"]) & set(fb["booleans"]))
                if fa["booleans"][key].isdisjoint(fb["booleans"][key])]
    if booleans:
        evidence["boolean_flip"] = booleans

    if bool(fa["supersede"]) != bool(fb["supersede"]):
        evidence["supersede_marker"] = sorted(fa["supersede"] ^ fb["supersede"])

    return evidence


def evaluate_features(fa: dict[str, Any], fb: dict[str, Any], cosine: float,
                      same_file: bool = False
                      ) -> Optional[tuple[float, dict[str, list[str]]]]:
    """(score, evidence), or None when nothing worth reporting fires.

    score = cosine x weighted signal mass (capped at 1.0). Both factors matter:
    cosine says "the same subject", the signals say "different claims".

    Two sections of the SAME note need a STRONG signal: within one document a
    differing number is nearly always a comparison table or a versioned recipe
    (`v3` next to `v4`), which is a deliberate juxtaposition, not a
    contradiction. The cost is real and accepted at stage 1 — a note that
    numerically contradicts itself is not reported."""
    evidence = _evidence_from_features(fa, fb)
    if not evidence:
        return None
    allowed = STRONG_SIGNALS if same_file else TRIGGER_SIGNALS
    if not (allowed & set(evidence)):
        return None
    mass = min(1.0, sum(SIGNAL_WEIGHTS[name] for name in evidence))
    return round(float(cosine) * mass, 4), evidence


def evaluate(a_text: str, b_text: str, cosine: float, same_file: bool = False
             ) -> Optional[tuple[float, dict[str, list[str]]]]:
    """Single-pair convenience wrapper over `features` + `evaluate_features`."""
    return evaluate_features(features(a_text), features(b_text), cosine, same_file)


# --- verdict journal (.mb/lint.json) ---------------------------------------

def pair_key(a_file: str, a_section: str, b_file: str, b_section: str) -> str:
    """Order-independent identity of a pair, so a verdict sticks regardless of
    which side the next scan happens to enumerate first."""
    left, right = f"{a_file}#{a_section}", f"{b_file}#{b_section}"
    return "||".join(sorted((left, right)))


def journal_path(cfg: Config) -> Path:
    return cfg.mb_dir / JOURNAL_NAME


def read_journal(cfg: Config) -> dict[str, Any]:
    """Tolerant, like `digest._read_journal`: a damaged journal reads as empty
    rather than breaking the scan."""
    path = journal_path(cfg)
    if not path.exists():
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "verdicts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "verdicts": {}}
    if not isinstance(data, dict) or not isinstance(data.get("verdicts"), dict):
        return {"schema_version": JOURNAL_SCHEMA_VERSION, "verdicts": {}}
    return data


def record_verdict(cfg: Config, key: str, verdict: str, note: str = "",
                   now: Optional[str] = None) -> dict[str, Any]:
    """Silence a pair for good. Without this the same false positive resurfaces
    on every run and the report stops being read at all."""
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict {verdict!r} "
                         f"(expected one of: {', '.join(VERDICTS)})")
    data = read_journal(cfg)
    data["schema_version"] = JOURNAL_SCHEMA_VERSION
    data["verdicts"][key] = {
        "verdict": verdict, "note": note,
        "at": now or datetime.now().isoformat(timespec="seconds")}
    atomic_write_json(journal_path(cfg), data)
    return {"key": key, "verdict": verdict, "total": len(data["verdicts"])}


# --- scanning ---------------------------------------------------------------

def _chunk_text(chunk: dict[str, Any]) -> str:
    return str(chunk.get("content") or "")


def _entry(a: dict[str, Any], b: dict[str, Any], score: float,
           cosine: float, evidence: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "key": pair_key(str(a.get("file", "")), str(a.get("section", "")),
                        str(b.get("file", "")), str(b.get("section", ""))),
        "a": {"file": a.get("file"), "section": a.get("section"),
              "source_id": a.get("source_id")},
        "b": {"file": b.get("file"), "section": b.get("section"),
              "source_id": b.get("source_id")},
        "score": score, "cosine": round(float(cosine), 4), "evidence": evidence,
    }


def _clean(entry: dict[str, Any]) -> dict[str, Any]:
    for side in ("a", "b"):
        if entry[side].get("source_id") is None:
            entry[side].pop("source_id")
    return entry


def _sorted_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic: score desc, then the pair key — a repeated run on an
    unchanged index is byte-identical."""
    entries.sort(key=lambda e: (-e["score"], e["key"]))
    return entries


def scan(cfg: Config, limit: int = DEFAULT_LIMIT,
         band: tuple[float, float] = CONTRADICTION_BAND,
         include_same_file: bool = False) -> dict[str, Any]:
    """In-band contradiction candidates across the VAULT corpus.

    Same-file pairs are OFF by default, mirroring how `curator.lint()` keeps
    same-file and cross-file duplicates apart. Measured on the real vault they
    are overwhelmingly document structure — a comparison table, a versioned
    recipe (`v3` beside `v4`), a spec that quotes both `always` and `never`
    while describing different tasks. Opt in with `--same-file` when auditing
    one long note; the report says which mode produced it."""
    from . import curator
    from .search import indexer
    from .search.similarity import blockwise_pairs

    low, high = band
    embs, chunks, status = indexer.load_committed_index(cfg.memory_index_dir)
    out: dict[str, Any] = {"index_status": status, "band": [low, high],
                           "candidates": [], "scanned_pairs": 0,
                           "suppressed": 0, "capped": False, "total": 0,
                           "include_same_file": include_same_file}
    if status != "ok":
        return out

    sub_embs, sub_chunks = curator._eligible_chunk_rows(chunks, embs)
    if sub_embs.shape[0] < 2:
        return out

    pairs = [(i, j, s) for i, j, s in blockwise_pairs(sub_embs, low) if s < high]
    out["scanned_pairs"] = len(pairs)
    if len(pairs) > CANDIDATE_PAIR_CAP:
        pairs.sort(key=lambda p: -p[2])
        pairs = pairs[:CANDIDATE_PAIR_CAP]
        out["capped"] = True

    judged = set(read_journal(cfg)["verdicts"])
    feature_cache: dict[int, dict[str, Any]] = {}

    def feats(index: int) -> dict[str, Any]:
        if index not in feature_cache:
            feature_cache[index] = features(_chunk_text(sub_chunks[index]))
        return feature_cache[index]

    entries: list[dict[str, Any]] = []
    suppressed = 0
    for i, j, cosine in pairs:
        a, b = sub_chunks[i], sub_chunks[j]
        same_file = a.get("file") == b.get("file")
        if same_file and (not include_same_file
                          or a.get("section") == b.get("section")):
            continue   # off by default; windows of one section are never claims
        verdict = evaluate_features(feats(i), feats(j), cosine, same_file=same_file)
        if verdict is None:
            continue
        score, evidence = verdict
        entry = _clean(_entry(a, b, score, cosine, evidence))
        if entry["key"] in judged:
            suppressed += 1
            continue
        entries.append(entry)

    out["suppressed"] = suppressed
    out["candidates"] = _sorted_entries(entries)[:limit] if limit else _sorted_entries(entries)
    out["total"] = len(entries)
    return out


def for_source(src_embs: np.ndarray, src_chunks: list[dict[str, Any]],
               src_rows: Iterable[int], mem_embs: np.ndarray,
               mem_chunks: list[dict[str, Any]], mem_rows: Iterable[int],
               judged: Optional[set] = None, limit: int = DEFAULT_LIMIT,
               band: tuple[float, float] = CONTRADICTION_BAND) -> list[dict[str, Any]]:
    """Cross-corpus variant used by `mb ingest plan`: does this source
    contradict something the wiki already claims?

    Takes already-loaded arrays rather than a Config so the planner does not
    re-read two index generations it is holding open."""
    low, high = band
    src_list, mem_list = list(src_rows), list(mem_rows)
    if not src_list or not mem_list:
        return []
    sim = src_embs[src_list] @ mem_embs[mem_list].T
    judged = judged or set()
    src_feats = {row: features(_chunk_text(src_chunks[row])) for row in src_list}
    mem_feats: dict[int, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    for si, mi in np.argwhere((sim > low) & (sim < high)):
        a_row, b_row = src_list[int(si)], mem_list[int(mi)]
        a, b = src_chunks[a_row], mem_chunks[b_row]
        if b_row not in mem_feats:
            mem_feats[b_row] = features(_chunk_text(b))
        # cross-corpus: never the same file, so the same-file strictness that
        # applies inside one note is not relevant here
        verdict = evaluate_features(src_feats[a_row], mem_feats[b_row],
                                    float(sim[si, mi]))
        if verdict is None:
            continue
        score, evidence = verdict
        entry = _clean(_entry(a, b, score, float(sim[si, mi]), evidence))
        if entry["key"] in judged:
            continue
        entries.append(entry)
    return _sorted_entries(entries)[:limit]


# ---------------------------------------------------------------------------

def run(cfg: Config, as_json: bool = False, limit: int = DEFAULT_LIMIT,
        include_same_file: bool = False) -> int:
    report = scan(cfg, limit=limit, include_same_file=include_same_file)
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
        return 0

    low, high = report["band"]
    print(f"== mb curator contradictions — cosine band ({low}, {high}) ==")
    scope = "cross-file + same-file" if report["include_same_file"] else "cross-file only"
    print(f"index:{report['index_status']}  in-band pairs:{report['scanned_pairs']}"
          f"  candidates:{report['total']}  suppressed:{report['suppressed']}"
          f"  scope:{scope}")
    if report["capped"]:
        print(f"[warn] pair cap {CANDIDATE_PAIR_CAP} reached — only the highest-cosine "
              f"pairs were scored; this report is NOT exhaustive")
    if not report["candidates"]:
        print("(no candidates)")
    for entry in report["candidates"]:
        print(f"\n {entry['score']:.2f} (cos {entry['cosine']:.2f})  {entry['key']}")
        print(f"   A: {entry['a']['file']} :: {entry['a']['section']}")
        print(f"   B: {entry['b']['file']} :: {entry['b']['section']}")
        for signal, tokens in sorted(entry["evidence"].items()):
            print(f"   {signal}: {', '.join(tokens[:6])}")
    print("\nThese are CANDIDATES, not verdicts — precision without an LLM is low.")
    print("Dismiss one for good: mb curator verdict <key> --verdict false_positive")
    return 0
