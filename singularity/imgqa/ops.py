"""imgqa in-process subcommands: probe, edges, backgrounds, compare, contrast.

Each `run_*` returns (inputs, data, verdict, outputs) for the CLI to wrap in
the contract envelope. Tool failures raise ContractError (-> exit 2).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ._contract import (ContractError, atomic_save_image, load_rgba, out_name,
                        parse_bbox, resolve_out)

# --- probe -----------------------------------------------------------------

SOLID_ALPHA = 200
TRANSLUCENT_LOW = 1
TRANSLUCENT_HIGH = 254


def _detect_baked_checkerboard(rgb: np.ndarray) -> tuple[bool, str]:
    """Detect the classic baked light-gray checkerboard on a flattened image.

    Heuristic (deterministic): in a corner crop, the two most common colors are
    both light and near-gray, cover almost everything, and the map of
    color-A-vs-rest is periodic along both axes with a period of 4..64 px.
    """
    h, w = rgb.shape[:2]
    crop = rgb[: min(h, 128), : min(w, 128)]
    flat = crop.reshape(-1, 3)
    colors, counts = np.unique(flat, axis=0, return_counts=True)
    if len(colors) < 2:
        return False, "single flat color"
    order = np.argsort(counts)[::-1]
    top2, top2_counts = colors[order[:2]], counts[order[:2]]
    coverage = top2_counts.sum() / flat.shape[0]
    if coverage < 0.90:
        return False, f"top-2 colors cover only {coverage:.0%}"
    for c in top2:
        if int(c.max()) - int(c.min()) > 12 or c.min() < 150:
            return False, "dominant colors are not light gray"
    mask = np.all(crop == top2[0], axis=-1)
    for axis in (0, 1):
        periodic = False
        for period in range(4, 65):
            if mask.shape[axis] < period * 2:
                break
            rolled = np.roll(mask, period, axis=axis)
            if (mask == rolled).mean() > 0.95 and not np.all(mask == np.roll(mask, 1, axis=axis)):
                periodic = True
                break
        if not periodic:
            return False, f"no periodicity along axis {axis}"
    return True, "two dominant light-gray colors in a periodic grid"


def run_probe(args):
    from PIL import Image
    path = Path(args.image)
    if not path.is_file():
        raise ContractError(f"input not found: {path}")
    im = Image.open(path)
    fmt, mode, size = im.format, im.mode, list(im.size)
    has_alpha = mode in ("RGBA", "LA", "PA") or "transparency" in im.info
    rgba = np.array(im.convert("RGBA"))
    alpha = rgba[..., 3]
    alpha_stats = {
        "min": int(alpha.min()), "max": int(alpha.max()),
        "mean": round(float(alpha.mean()), 2),
        "pct_translucent": round(float(
            ((alpha >= TRANSLUCENT_LOW) & (alpha <= TRANSLUCENT_HIGH)).mean()) * 100, 2),
    }
    baked, evidence = (False, "image has a real alpha channel")
    if not has_alpha or alpha_stats["pct_translucent"] == 0:
        baked, evidence = _detect_baked_checkerboard(rgba[..., :3])
    usable = has_alpha and not baked
    data = {"format": fmt, "mode": mode, "size": size, "has_alpha": has_alpha,
            "alpha_stats": alpha_stats, "baked_checkerboard": baked,
            "checkerboard_evidence": evidence}
    verdict = {"result": "PASS" if usable else "FAIL",
               "reason": None if usable else
               ("baked checkerboard background" if baked else "no usable alpha channel")}
    return {"image": str(path.resolve())}, data, verdict, None


# --- edges -------------------------------------------------------------------

def _boundary_cells(alpha: np.ndarray, cell: int = 128):
    solid = alpha >= SOLID_ALPHA
    if not solid.any():
        raise ContractError("no solid alpha region — nothing to crop edges from")
    from scipy.ndimage import binary_dilation, binary_erosion
    boundary = binary_dilation(solid) & ~binary_erosion(solid)
    ys, xs = np.nonzero(boundary)
    cells = {}
    for y, x in zip(ys.tolist(), xs.tolist()):
        cells.setdefault((y // cell, x // cell), 0)
        cells[(y // cell, x // cell)] += 1
    return cells, cell


def run_edges(args):
    path = Path(args.image)
    im = load_rgba(path)
    alpha = np.array(im)[..., 3]
    out_dir = resolve_out(args.out, [path])
    cells, cell = _boundary_cells(alpha)
    ranked = sorted(cells.items(), key=lambda kv: kv[1], reverse=True)
    picks, taken = [], []
    for (cy, cx), _count in ranked:
        if len(picks) >= args.max_regions:
            break
        if any(abs(cy - ty) <= 1 and abs(cx - tx) <= 1 for ty, tx in taken):
            continue
        picks.append((cy, cx))
        taken.append((cy, cx))
    regions = []
    for idx, (cy, cx) in enumerate(sorted(picks), start=1):
        y0, x0 = cy * cell, cx * cell
        y1, x1 = min(y0 + cell, alpha.shape[0]), min(x0 + cell, alpha.shape[1])
        crop = im.crop((x0, y0, x1, y1))
        crop = crop.resize((crop.width * 2, crop.height * 2), resample=0)  # nearest, 200%
        dest = out_dir / out_name(path, "edges", f"r{idx:02d}", "png")
        atomic_save_image(crop, dest)  # PNG = lossless, per NIKA lesson
        regions.append({"bbox": [x0, y0, x1 - x0, y1 - y0], "path": str(dest)})
    return ({"image": str(path.resolve()), "out": str(out_dir)},
            {"regions": regions}, None, [r["path"] for r in regions])


# --- backgrounds --------------------------------------------------------------

BACKGROUNDS = [("black", (0, 0, 0)), ("pink", (255, 192, 203)), ("white", (255, 255, 255))]


def run_backgrounds(args):
    from PIL import Image
    path = Path(args.image)
    im = load_rgba(path)
    out_dir = resolve_out(args.out, [path])
    renders, outputs = [], []
    for label, rgb in BACKGROUNDS:
        base = Image.new("RGBA", im.size, rgb + (255,))
        base.alpha_composite(im)
        dest = out_dir / out_name(path, "backgrounds", label, "png")
        atomic_save_image(base.convert("RGB"), dest)
        renders.append({"background": label, "path": str(dest)})
        outputs.append(str(dest))
    return ({"image": str(path.resolve()), "out": str(out_dir)},
            {"renders": renders}, None, outputs)


# --- compare -------------------------------------------------------------------

def run_compare(args):
    path_a, path_b = Path(args.image_a), Path(args.image_b)
    im_a, im_b = load_rgba(path_a), load_rgba(path_b)
    inputs = {"image_a": str(path_a.resolve()), "image_b": str(path_b.resolve()),
              "threshold": args.threshold, "bbox": args.bbox,
              "max_mae": args.max_mae, "max_diff_ratio": args.max_diff_ratio}
    if im_a.size != im_b.size:
        data = {"size_a": list(im_a.size), "size_b": list(im_b.size)}
        return inputs, data, {"result": "FAIL", "reason": "size-mismatch"}, None
    arr_a = np.asarray(im_a, dtype=np.int16)
    arr_b = np.asarray(im_b, dtype=np.int16)
    if args.bbox:
        x0, y0, x1, y1 = parse_bbox(args.bbox, image_size=im_a.size)
        arr_a, arr_b = arr_a[y0:y1, x0:x1], arr_b[y0:y1, x0:x1]
    delta = np.abs(arr_a - arr_b)
    mae = round(float(delta.mean()), 4)
    max_delta = int(delta.max())
    diff_mask = (delta > args.threshold).any(axis=-1)
    diff_pixels = int(diff_mask.sum())
    diff_ratio = round(float(diff_mask.mean()), 6)
    data = {"mae": mae, "max_delta": max_delta, "diff_pixels": diff_pixels,
            "diff_ratio": diff_ratio, "threshold": args.threshold}
    if args.max_mae is not None or args.max_diff_ratio is not None:
        if args.max_mae is None or args.max_diff_ratio is None:
            raise ContractError("--max-mae and --max-diff-ratio must be given together (contract §4)")
        ok = mae <= args.max_mae and diff_ratio <= args.max_diff_ratio
        verdict = {"result": "PASS" if ok else "FAIL",
                   "reason": None if ok else
                   f"mae {mae} vs {args.max_mae}, diff_ratio {diff_ratio} vs {args.max_diff_ratio}"}
    else:
        verdict = {"result": "PASS", "reason": "metrics-only run (no gate flags)"}
    return inputs, data, verdict, None


# --- contrast -------------------------------------------------------------------

def _srgb_to_lin(c: float) -> float:
    c = c / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _rel_lum(hexcolor: str) -> float:
    hexcolor = hexcolor.lstrip("#")
    if len(hexcolor) != 6:
        raise ContractError(f"bad color {hexcolor!r}: expected RRGGBB hex")
    try:
        r, g, b = (int(hexcolor[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise ContractError(f"bad color {hexcolor!r}: expected RRGGBB hex") from exc
    return 0.2126 * _srgb_to_lin(r) + 0.7152 * _srgb_to_lin(g) + 0.0722 * _srgb_to_lin(b)


def run_contrast(args):
    l1, l2 = _rel_lum(args.fg), _rel_lum(args.bg)
    lighter, darker = max(l1, l2), min(l1, l2)
    # gate on the RAW ratio; round only the display field. 777777/070707 is
    # 4.4983… — rounding to 4.50 before comparing produced a false AA PASS (F6)
    raw = (lighter + 0.05) / (darker + 0.05)
    ratio = round(raw, 2)
    data = {"ratio": ratio, "ratio_raw": raw, "aa_normal": raw >= 4.5,
            "aa_large": raw >= 3.0, "aaa_normal": raw >= 7.0}
    ok = raw >= args.min
    verdict = {"result": "PASS" if ok else "FAIL",
               "reason": None if ok else f"ratio {raw:.4f} below required {args.min}"}
    return {"fg": args.fg, "bg": args.bg, "min": args.min}, data, verdict, None
