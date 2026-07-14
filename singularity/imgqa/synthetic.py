"""Deterministic synthetic fixtures (WSF-01d tier 1).

Used by both `imgqa selftest` and the pytest suite — zero binary fixtures
needed for this tier; every image is generated on the fly, bit-identically.
"""
from __future__ import annotations

import numpy as np


def make_cutout(size: int = 256) -> np.ndarray:
    """RGBA cutout with: solid disc, dense soft halo band at x>160 (a
    removal-tool fringe that MUST be detected) and thin hair-like strands at
    x<150 (legitimate matting that must NOT be detected)."""
    yy, xx = np.mgrid[0:size, 0:size]
    arr = np.zeros((size, size, 4), dtype=np.uint8)
    arr[..., 0] = 200
    disc = (yy - 128) ** 2 + (xx - 100) ** 2 <= 50 ** 2
    arr[..., 3][disc] = 255
    halo = ((yy - 128) ** 2 + (xx - 100) ** 2 <= 110 ** 2) & (xx > 160)
    arr[..., 3][halo & ~disc] = 90
    for i, y in enumerate(range(20, 60, 4)):
        seg = arr[..., 3][y, 30:min(90 + i * 5, 250)]
        arr[..., 3][y, 30:min(90 + i * 5, 250)] = np.where(seg == 0, 90, seg)
    return arr


def make_checkerboard(size: int = 512, cell: int = 16,
                      light: int = 255, dark: int = 204) -> np.ndarray:
    """24bpp baked checkerboard (the classic flattened-transparency look) —
    probe must report baked_checkerboard=true (GT-PR2)."""
    yy, xx = np.mgrid[0:size, 0:size]
    board = ((yy // cell + xx // cell) % 2)
    arr = np.where(board[..., None] == 0, np.uint8(light), np.uint8(dark))
    return (arr * np.ones((size, size, 3), np.uint8))
