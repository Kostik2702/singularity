"""Matching clips that were graded differently, and the creative look on top.

Generated footage drifts: the same character comes back warmer, darker, more
saturated from one batch to the next. Cutting that together reads as sloppiness
even when every individual shot is beautiful.

The fix is two separate layers, and keeping them separate is the whole point:

  MATCH   a per-clip linear correction that moves each clip's per-channel mean
          and spread onto a common target. Purely corrective, no taste in it.
  LOOK    one creative grade applied to the whole film AFTER matching, so it
          lands identically on every shot.

Matching uses `colorlevels`, which is exactly a per-channel linear map: feeding
it the clip's own [mean - k*std, mean + k*std] as the input range and the
target's as the output range makes the two distributions coincide.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .util import probe_json, run

# Creative looks. Applied once, to the matched sequence.
LOOKS: dict[str, str] = {
    "none": "",
    # Split-toned gothic: blacks crushed and pushed COLD, highlights toward
    # ember. Warming the shadows instead (the obvious first try) turns every
    # night exterior into sepia mud — the cold/warm split is what reads as
    # 18th-century gothic rather than as an old photograph.
    "gothic-ember": (
        "curves=r='0/0 0.25/0.19 0.5/0.52 0.75/0.83 1/1':"
        "g='0/0 0.25/0.17 0.5/0.47 0.75/0.76 1/0.96':"
        "b='0/0.03 0.25/0.21 0.5/0.45 0.75/0.69 1/0.90',"
        "eq=contrast=1.14:saturation=0.86:gamma=0.95,"
        "colorbalance=rs=-0.05:gs=-0.01:bs=0.07:rm=0.02:bm=-0.01:rh=0.08:gh=0.02:bh=-0.07"
    ),
    # colder, bluer, for moonlit material
    "gothic-moon": (
        "curves=r='0/0 0.5/0.46 1/0.94':g='0/0 0.5/0.49 1/0.97':"
        "b='0/0.03 0.5/0.55 1/1',"
        "eq=contrast=1.10:saturation=0.80,"
        "colorbalance=rs=-0.04:bs=0.08:rm=-0.02:bm=0.05"
    ),
    "bleach-bone": "eq=contrast=1.18:saturation=0.55:gamma=0.94",
    # Fashion-film neutral: blacks lifted a hair (matte, not crushed), skin left
    # alone, everything else cooled and pulled down. Restraint is the look — a
    # heavy grade on an editorial cut reads as a filter, not as art direction.
    "editorial-cool": (
        "curves=r='0/0.02 0.5/0.49 1/0.98':g='0/0.02 0.5/0.50 1/0.99':"
        "b='0/0.04 0.5/0.52 1/1',"
        "eq=contrast=1.07:saturation=0.90:gamma=1.01,"
        "colorbalance=rs=-0.03:bs=0.05:rm=0.01:bm=0.01:rh=0.02:bh=-0.02"
    ),
}

# Texture applied last: grain + vignette. Sold separately from the look so a
# shot can be matched and graded without being dirtied.
def texture(grain: float = 8.0, vignette: float = 0.0) -> str:
    parts = []
    if vignette > 0:
        parts.append(f"vignette=PI/{vignette:g}")
    if grain > 0:
        parts.append(f"noise=alls={grain:g}:allf=t+u")
    return ",".join(parts)


def stats(path: str | Path, samples: int = 9) -> dict[str, Any]:
    """Per-channel mean and std over evenly spaced frames, in 0..1.

    Sampled rather than exhaustive: colour drift is a property of the grade, not
    of individual frames, and nine samples across a clip settle the mean to well
    inside the precision the correction needs."""
    import numpy as np

    j = probe_json(path)
    v = next(s for s in j["streams"] if s.get("codec_type") == "video")
    w, h = int(v["width"]), int(v["height"])
    dur = float(j["format"].get("duration") or 0)
    if dur <= 0:
        raise ValueError(f"{path}: unknown duration")

    acc = []
    for i in range(samples):
        t = dur * (i + 0.5) / samples
        raw = run(["ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
                   "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        if len(raw) < w * h * 3:
            continue
        a = np.frombuffer(raw[:w * h * 3], dtype=np.uint8).reshape(-1, 3).astype(np.float32) / 255.0
        acc.append(a)
    if not acc:
        raise ValueError(f"{path}: no frames decoded")
    allpix = np.concatenate(acc, axis=0)
    return {
        "path": str(path),
        "mean": [round(float(allpix[:, c].mean()), 5) for c in range(3)],
        "std": [round(float(allpix[:, c].std()), 5) for c in range(3)],
        "luma": round(float((0.2126 * allpix[:, 0] + 0.7152 * allpix[:, 1]
                             + 0.0722 * allpix[:, 2]).mean()), 5),
        "saturation": round(float((allpix.max(axis=1) - allpix.min(axis=1)).mean()), 5),
    }


def target_from(all_stats: Iterable[dict[str, Any]], *,
                reference: str | None = None) -> dict[str, Any]:
    """The common look to move everything onto.

    Median, not mean: one wildly-graded outlier should not drag the whole film
    toward itself. A `reference` clip overrides it when the owner has already
    decided which shot is right."""
    rows = list(all_stats)
    if reference:
        for r in rows:
            if Path(r["path"]).name == reference or r["path"] == reference:
                return {"mean": r["mean"], "std": r["std"], "from": r["path"]}
    def med(key: str, c: int) -> float:
        vals = sorted(r[key][c] for r in rows)
        n = len(vals)
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
    return {"mean": [round(med("mean", c), 5) for c in range(3)],
            "std": [round(med("std", c), 5) for c in range(3)],
            "from": f"median of {len(rows)} clips"}


def levels_filter(clip: dict[str, Any], target: dict[str, Any], *,
                  k: float = 2.0, strength: float = 1.0, mode: str = "luma") -> str:
    """`colorlevels` mapping this clip's distribution onto the target's.

    `mode="luma"` is the default, and it is the one to reach for on a film whose
    scenes are legitimately different colours. It equalises EXPOSURE and
    CONTRAST only — the same input/output range on all three channels — so a
    cold storm stays cold and a black-and-red insert stays black and red, while
    nothing is glaringly brighter than its neighbour. Palette unity then comes
    from ONE creative look over the whole film, which is how it is done in
    grading rooms.

    `mode="rgb"` matches each channel independently. It genuinely unifies colour
    but will drag a deliberately-tinted scene toward the average: measured on
    this material it turned a blue-grey graveyard sepia and put a teal cast on a
    black-and-red guillotine. Use it only when the clips are meant to be the
    same look and drifted by accident.

    `strength` < 1 pulls only part of the way and keeps intent.
    """
    args = []
    names = ("r", "g", "b")
    if mode == "luma":
        lc = 0.2126 * clip["mean"][0] + 0.7152 * clip["mean"][1] + 0.0722 * clip["mean"][2]
        sc_all = 0.2126 * clip["std"][0] + 0.7152 * clip["std"][1] + 0.0722 * clip["std"][2]
        lt = 0.2126 * target["mean"][0] + 0.7152 * target["mean"][1] + 0.0722 * target["mean"][2]
        st_all = 0.2126 * target["std"][0] + 0.7152 * target["std"][1] + 0.0722 * target["std"][2]
        clip = {"mean": [lc] * 3, "std": [max(sc_all, 1e-4)] * 3}
        target = {"mean": [lt] * 3, "std": [max(st_all, 1e-4)] * 3}
    for c, n in enumerate(names):
        mc, sc = clip["mean"][c], max(clip["std"][c], 1e-4)
        mt, st = target["mean"][c], max(target["std"][c], 1e-4)
        mt = mc + (mt - mc) * strength
        st = sc + (st - sc) * strength
        imin, imax = mc - k * sc, mc + k * sc
        omin, omax = mt - k * st, mt + k * st
        # colorlevels needs 0..1 and a non-degenerate input range
        imin, imax = max(0.0, min(imin, 0.98)), min(1.0, max(imax, imin + 0.02))
        omin, omax = max(0.0, min(omin, 0.98)), min(1.0, max(omax, omin + 0.02))
        args += [f"{n}imin={imin:.4f}", f"{n}imax={imax:.4f}",
                 f"{n}omin={omin:.4f}", f"{n}omax={omax:.4f}"]
    return "colorlevels=" + ":".join(args)


def build_map(paths: list[str | Path], *, reference: str | None = None,
              k: float = 2.0, strength: float = 0.8, mode: str = "luma",
              samples: int = 9) -> dict[str, Any]:
    rows, problems = [], []
    for p in paths:
        try:
            rows.append(stats(p, samples))
        except Exception as exc:
            problems.append(f"{p}: {exc}")
    if not rows:
        return {"problems": problems or ["no clips measured"]}
    target = target_from(rows, reference=reference)
    out = {}
    for r in rows:
        drift = math.sqrt(sum((r["mean"][c] - target["mean"][c]) ** 2 for c in range(3)))
        out[r["path"]] = {
            "filter": levels_filter(r, target, k=k, strength=strength, mode=mode),
            "mean": r["mean"], "std": r["std"], "luma": r["luma"],
            "saturation": r["saturation"], "drift_from_target": round(drift, 4),
        }
    return {"target": target, "k": k, "strength": strength, "mode": mode,
            "clips": out, "problems": problems}


def save_map(m: dict[str, Any], path: str | Path) -> None:
    Path(path).write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")


def load_map(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
