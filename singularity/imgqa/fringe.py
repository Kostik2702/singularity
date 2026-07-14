"""detect-fringe + defringe (CONTRACT.md §4).

detect-fringe — deterministic detector of wide soft alpha halos ("fringe")
left by background-removal tools. The discriminator that keeps fine hair
matting safe (GT-N2): hair strands are thin, so within a small grid cell the
fraction of "suspect" pixels stays low; a removal-tool halo is a DENSE band of
translucent pixels far from the solid silhouette. We flag only dense clusters.

defringe — parameterized port of NIKA cleanup_alpha_fringe.py (steep alpha
remap + light re-blur, blended back through a soft-edged rect mask) applied
ONLY inside explicit bboxes, never globally.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from ._contract import (ContractError, atomic_save_image, load_rgba, out_name,
                        parse_bbox, resolve_out)

# Detector tunables (implementation detail; the frozen thing is the GT table).
SOFT_LOW = 5
SOFT_HIGH = 180
SOLID_ALPHA = 200
MIN_DIST_FROM_SOLID = 6      # px; closer than this is normal anti-aliasing
CELL = 16                    # px grid
CELL_SUSPECT_FRAC = 0.35     # dense-cell threshold — hair strands stay below
MIN_CLUSTER_CELLS = 4        # ignore isolated speckle cells
MIN_DETECTION_SCORE = 0.8    # removal-tool halo is a near-UNIFORM band (cluster
                             # mean frac ~0.9); legitimate hair/fabric clusters
                             # are structured and land at ~0.5-0.72 (measured on
                             # NIKA GT: real boot defect 0.896 vs six legit
                             # translucent regions <=0.724 on the owner-accepted
                             # clean image)


def run_detect_fringe(args):
    from scipy.ndimage import distance_transform_edt, label
    path = Path(args.image)
    im = load_rgba(path)
    alpha = np.array(im)[..., 3]
    solid = alpha >= SOLID_ALPHA
    if not solid.any():
        raise ContractError("no solid alpha region — not a cutout image?")
    soft = (alpha >= SOFT_LOW) & (alpha <= SOFT_HIGH)
    dist_to_solid = distance_transform_edt(~solid)
    suspect = soft & (dist_to_solid > MIN_DIST_FROM_SOLID)

    h, w = alpha.shape
    ch, cw = (h + CELL - 1) // CELL, (w + CELL - 1) // CELL
    frac = np.zeros((ch, cw), dtype=np.float32)
    for cy in range(ch):
        for cx in range(cw):
            block = suspect[cy * CELL:(cy + 1) * CELL, cx * CELL:(cx + 1) * CELL]
            frac[cy, cx] = block.mean() if block.size else 0.0
    flagged = frac >= CELL_SUSPECT_FRAC
    labels, n = label(flagged)
    detections = []
    for i in range(1, n + 1):
        ys, xs = np.nonzero(labels == i)
        if len(ys) < MIN_CLUSTER_CELLS:
            continue
        x0, y0 = int(xs.min()) * CELL, int(ys.min()) * CELL
        x1 = min((int(xs.max()) + 1) * CELL, w)
        y1 = min((int(ys.max()) + 1) * CELL, h)
        score = round(float(frac[ys, xs].mean()), 3)
        if score < MIN_DETECTION_SCORE:
            continue
        detections.append({"bbox": [x0, y0, x1 - x0, y1 - y0],
                           "score": score, "kind": "fringe"})
    detections.sort(key=lambda d: d["score"], reverse=True)
    data = {"detections": detections,
            "params": {"min_score": MIN_DETECTION_SCORE,
                       "soft": [SOFT_LOW, SOFT_HIGH], "min_dist": MIN_DIST_FROM_SOLID,
                       "cell": CELL, "cell_frac": CELL_SUSPECT_FRAC,
                       "min_cluster_cells": MIN_CLUSTER_CELLS}}
    clean = not detections
    verdict = {"result": "PASS" if clean else "FAIL",
               "reason": None if clean else f"{len(detections)} fringe region(s) detected"}
    return {"image": str(path.resolve())}, data, verdict, None


def _sharpen_alpha(alpha: np.ndarray, low: int, high: int) -> np.ndarray:
    from scipy.ndimage import gaussian_filter
    a = alpha.astype(np.float32)
    a = np.clip((a - low) * (255.0 / (high - low)), 0, 255)
    a = gaussian_filter(a, sigma=0.8)  # ~1px re-feather for clean AA
    return a.astype(np.uint8)


def _soft_rect_mask(shape, box, feather: float) -> np.ndarray:
    from scipy.ndimage import gaussian_filter
    h, w = shape
    x0, y0, x1, y1 = box
    mask = np.zeros((h, w), dtype=np.float32)
    mask[max(y0, 0):min(y1, h), max(x0, 0):min(x1, w)] = 1.0
    return gaussian_filter(mask, sigma=feather)


def run_defringe(args):
    path = Path(args.image)
    if not args.bbox:
        raise ContractError("at least one --bbox X,Y,W,H is required — defringe is never global")
    im = load_rgba(path)
    boxes = [parse_bbox(spec, image_size=im.size) for spec in args.bbox]
    out_dir = resolve_out(args.out, [path])
    arr = np.array(im)
    alpha = arr[..., 3]
    h, w = alpha.shape
    combined = np.zeros((h, w), dtype=np.float32)
    for box in boxes:
        combined = np.maximum(combined, _soft_rect_mask((h, w), box, args.feather))
    fixed = _sharpen_alpha(alpha, args.low, args.high)
    arr[..., 3] = np.clip(
        alpha.astype(np.float32) * (1 - combined) + fixed.astype(np.float32) * combined,
        0, 255).astype(np.uint8)
    from PIL import Image
    dest = out_dir / out_name(path, "defringe", None, "png")
    atomic_save_image(Image.fromarray(arr, mode="RGBA"), dest)
    data = {"bboxes": [[b[0], b[1], b[2] - b[0], b[3] - b[1]] for b in boxes],
            "feather": args.feather, "remap": [args.low, args.high]}
    return ({"image": str(path.resolve()), "out": str(out_dir)},
            data, None, [str(dest)])
