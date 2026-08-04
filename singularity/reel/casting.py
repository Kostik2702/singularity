"""Casting: which clip is allowed into the film, and where it sits in the story.

Two failures this exists to stop, both invisible to every other check in the
pipeline because the cut is on the beat, the axis holds and the colour matches:

  CAUSALITY   the world un-happens. Ground cracks, a hand bursts out of it, and
              then the intact crack is on screen again. Measured on the Dead
              Revolutionary edit; nothing flagged it, because nothing knew what
              the shots MEANT.
  DEAD WEIGHT a generated batch always contains takes that cannot be used at
              all — wrong character, broken anatomy, an artefact through the
              middle. Without somewhere to record "rejected, because…", they get
              quietly re-litigated every time the edit is touched.

So the casting sheet is filled in ONCE, while the contact sheets are being read,
and everything downstream reads it instead of re-deciding. It is the only place
in the pipeline that holds meaning rather than measurement — which is exactly
why a human or the skill fills it, and the engine only checks it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VERDICTS = ("use", "partial", "reject", "?")


def scaffold(clips: list[dict[str, Any]], *, story: list[dict[str, Any]] | None = None
             ) -> dict[str, Any]:
    """An empty casting sheet: every clip listed, nothing decided."""
    out: dict[str, Any] = {
        "story": story or [],
        "clips": {},
        "_howto": {
            "verdict": "use | partial | reject | ?  — '?' means nobody has looked yet",
            "stage": "position in the STORY, not the timeline. Ground intact 1, "
                     "cracking 2, hand out 3, figure standing 4. Shots may not "
                     "go backwards through these unless marked reprise.",
            "usable": "[from, to] seconds inside the clip that are actually good",
            "depicts": "storyboard shot id this clip covers",
        },
    }
    for c in clips:
        out["clips"][c["name"]] = {
            "path": c["path"],
            "duration": c.get("duration"),
            "verdict": "?",
            "depicts": "",
            "stage": None,
            "segments": [],
            "usable": [0.0, round(float(c.get("duration") or 0), 2)],
            "note": "",
        }
    return out


def stage_at(rec: dict[str, Any], t: float) -> int | None:
    """Story stage at second `t` inside a clip.

    A six-second take routinely spans several stages — she approaches, she
    turns, she leaves. A stage recorded per FILE is therefore wrong the moment a
    clip is used twice at different points, which is exactly when the causality
    check matters. `segments` wins; the flat `stage` is the fallback for takes
    that really do sit in one state throughout."""
    for seg in rec.get("segments") or []:
        if float(seg["from"]) <= t < float(seg["to"]):
            return int(seg["stage"])
    return rec.get("stage")


def load(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save(sheet: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(sheet, ensure_ascii=False, indent=2),
                          encoding="utf-8")


def undecided(sheet: dict[str, Any]) -> list[str]:
    return [n for n, r in sheet.get("clips", {}).items() if r.get("verdict") == "?"]


def apply_to(sheet: dict[str, Any], edl) -> list[str]:
    """Push casting decisions into an EDL and report what the sheet forbids.

    Returns problems; also fills in `stage` on shots that did not carry one, so
    the causality check in `EDL.validate` has something to work with."""
    problems: list[str] = []
    by_path = {r.get("path"): (name, r) for name, r in sheet.get("clips", {}).items()}
    for i, shot in enumerate(edl.shots, 1):
        path = edl.sources.get(shot.source)
        hit = by_path.get(path)
        if hit is None:
            problems.append(f"S{i}: source '{shot.source}' is not in the casting sheet")
            continue
        name, rec = hit
        if rec.get("verdict") == "reject":
            problems.append(
                f"S{i} uses '{name}', which the casting sheet rejects"
                + (f": {rec['note']}" if rec.get("note") else ""))
        elif rec.get("verdict") == "?":
            problems.append(f"S{i} uses '{name}', which nobody has judged yet "
                            "(verdict '?')")
        if shot.stage is None:
            st = stage_at(rec, shot.in_sec)
            if st is not None:
                shot.stage = int(st)
        lo, hi = (rec.get("usable") or [None, None])[:2]
        if rec.get("verdict") == "partial" and lo is not None:
            n = edl.durations[i - 1]
            read = max(1, round(n * shot.source_frames_per_output))
            a = edl.in_frame(shot)
            fps = edl.grid.fps
            if a < lo * fps - 1 or (a + read) > hi * fps + 1:
                problems.append(
                    f"S{i} reads {a / fps:.2f}..{(a + read) / fps:.2f}s of '{name}' "
                    f"but only {lo}..{hi}s is marked usable")
    return problems


def story_table(sheet: dict[str, Any]) -> str:
    rows = ["stage  clip            verdict   depicts  note"]
    for name, r in sorted(sheet.get("clips", {}).items(),
                          key=lambda kv: (kv[1].get("stage") is None,
                                          kv[1].get("stage") or 0, kv[0])):
        rows.append("%-6s %-15s %-9s %-8s %s"
                    % (r.get("stage") if r.get("stage") is not None else "-",
                       name[:15], r.get("verdict"), r.get("depicts") or "-",
                       (r.get("note") or "")[:44]))
    return "\n".join(rows)
