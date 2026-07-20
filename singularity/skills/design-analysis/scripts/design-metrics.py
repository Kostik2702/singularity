"""design-metrics — deterministic numbers for mockup analysis (WSF-06).

Gives the design-analysis skill its EVIDENCE: exact dimensions, dominant
palette and horizontal band candidates (section boundaries by row-color
change). The aesthetic JUDGMENT (what a band MEANS, typography, accents)
is the skill's LLM step and must cite these numbers, not free-form guesses.
Mockup TEXT/figures are art-direction, never facts (UNVERIFIED rule) —
this tool deliberately extracts no text.

Envelope design-metrics/1; informational: exit 0 = metrics produced,
exit 2 = tool error. Pillow comes from the engine [imgqa] extras.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SCHEMA = "design-metrics/1"
MAX_PIXELS = 120_000_000  # imgqa resource-guard mirror
PALETTE_COLORS = 8
BAND_MIN_SHARE = 0.02     # bands under 2% of height are noise, merged
DELTA_SIGMA = 2.0         # boundary = row delta above mean + 2*std


class ToolError(Exception):
    pass


def load_image(path: Path):
    try:
        from PIL import Image
    except ImportError as exc:
        raise ToolError("Pillow missing — install engine extras [imgqa]") from exc
    if not path.is_file():
        raise ToolError(f"input not found: {path}")
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        im = Image.open(path)
        if im.width * im.height > MAX_PIXELS:
            raise ToolError(f"image {im.width}x{im.height} exceeds resource guard")
        return im.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ToolError(f"decompression bomb guard: {exc}") from exc
    except OSError as exc:
        raise ToolError(f"cannot decode {path}: {exc}") from exc


def hex_color(rgb) -> str:
    r, g, b = (int(v) for v in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


def dominant_colors(im) -> list[dict]:
    small = im.resize((min(im.width, 256), min(im.height, 256)))
    quant = small.quantize(colors=PALETTE_COLORS, dither=0)
    palette = quant.getpalette()
    counts = sorted(quant.getcolors() or [], reverse=True)
    total = sum(c for c, _ in counts) or 1
    out = []
    for count, idx in counts[:PALETTE_COLORS]:
        rgb = palette[idx * 3: idx * 3 + 3]
        out.append({"hex": hex_color(rgb), "share_pct": round(100 * count / total, 1)})
    return out


def band_candidates(im) -> list[dict]:
    """Horizontal section candidates: rows where the mean row color changes
    sharply. Deterministic: fixed downscale, threshold mean + DELTA_SIGMA*std,
    bands under BAND_MIN_SHARE of height merged into their neighbour."""
    import numpy as np
    thumb = im.resize((64, im.height))  # keep full row resolution
    arr = np.asarray(thumb, dtype=float)
    rows = arr.mean(axis=1)                       # (H, 3) mean color per row
    deltas = np.abs(np.diff(rows, axis=0)).sum(axis=1)
    if len(deltas) == 0:
        return [{"top": 0, "bottom": im.height, "share_pct": 100.0,
                 "avg_color": hex_color(rows[0])}]
    threshold = deltas.mean() + DELTA_SIGMA * deltas.std()
    cuts = [int(i) + 1 for i in np.flatnonzero(deltas > threshold)]
    # collapse runs of adjacent cut rows (a gradient edge yields several)
    boundaries = []
    for cut in cuts:
        if not boundaries or cut - boundaries[-1] > 4:
            boundaries.append(cut)
    edges = [0] + boundaries + [im.height]
    min_h = max(1, int(im.height * BAND_MIN_SHARE))
    bands: list[dict] = []
    for top, bottom in zip(edges, edges[1:]):
        if bands and bottom - top < min_h:
            bands[-1]["bottom"] = bottom  # merge noise band into neighbour
            continue
        bands.append({"top": top, "bottom": bottom})
    for band in bands:
        seg = rows[band["top"]:band["bottom"]]
        band["height"] = band["bottom"] - band["top"]
        band["share_pct"] = round(100 * band["height"] / im.height, 1)
        band["avg_color"] = hex_color(seg.mean(axis=0) if len(seg) else rows[0])
    return bands


def atomic_write(text: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(prog="design-metrics", description=__doc__)
    parser.add_argument("image", help="mockup PNG/JPG")
    parser.add_argument("--out", help="optional dir for metrics.json copy")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    env: dict = {"schema": SCHEMA, "command": "metrics",
                 "inputs": {"image": args.image}}
    try:
        path = Path(args.image)
        im = load_image(path)
        data = {
            "width": im.width,
            "height": im.height,
            "aspect_h_over_w": round(im.height / im.width, 2),
            "dominant_colors": dominant_colors(im),
            "bands": band_candidates(im),
            "params": {"palette_colors": PALETTE_COLORS,
                       "band_min_share": BAND_MIN_SHARE,
                       "delta_sigma": DELTA_SIGMA},
        }
        outputs = []
        if args.out:
            out_dir = Path(args.out).resolve()
            src_dir = path.resolve().parent
            if out_dir == src_dir or out_dir in src_dir.parents or src_dir in out_dir.parents:
                raise ToolError(f"--out {out_dir} is inside/above the mockup tree — forbidden")
            dest = out_dir / (path.stem + ".design-metrics.json")
            atomic_write(json.dumps({"schema": SCHEMA, **data},
                                    ensure_ascii=False, indent=1), dest)
            outputs.append(str(dest))
        env.update(status="ok", data=data, outputs=outputs)
        rc = 0
    except ToolError as exc:
        env.update(status="error", error=str(exc))
        rc = 2
    except Exception as exc:  # noqa: BLE001
        env.update(status="error", error=f"unexpected {type(exc).__name__}: {exc}")
        rc = 2
    print(json.dumps(env, ensure_ascii=False) if args.json else
          f"[design-metrics] {env['status']}"
          + (f" | {env['data']['width']}x{env['data']['height']}, "
             f"{len(env['data']['bands'])} bands" if env["status"] == "ok" else
             f" | error: {env['error']}"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
