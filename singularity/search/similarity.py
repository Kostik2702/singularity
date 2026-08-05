"""Shared vector math for the report-only analyzers (W05).

Three copies of mean-pooling and two of the blockwise cosine had accumulated
across `digest.py`, `curator.py` and (about to be) `ingest_plan.py` /
`contradictions.py`. They are identical by intent and must stay identical by
construction: `mb ingest plan` compares source chunks against the very note
vectors `mb curator` builds, so a drift between two copies would silently
produce two different notions of "similar".

Pure numpy, no model, no I/O — every caller passes embeddings it already read
from a committed index generation.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any
from collections.abc import Callable, Iterable

import numpy as np

# tile size for the blockwise cosine — bounds peak memory to O(block^2) rather
# than O(n^2) (a full 20k x 20k float64 matrix would be ~3GB)
BLOCK_SIZE = 512


def mean_pool(embs: np.ndarray, idxs: list[int]) -> np.ndarray | None:
    """Mean of the given chunk rows, re-normalized to unit length. None when
    the mean degenerates to the zero vector (no meaningful direction)."""
    v = embs[idxs].mean(axis=0)
    norm = np.linalg.norm(v)
    if norm == 0:
        return None
    return v / norm


def group_indices(chunks: list[dict[str, Any]],
                  key: Callable[[dict[str, Any]], Any] = lambda c: c.get("file"),
                  predicate: Callable[[dict[str, Any]], bool] | None = None
                  ) -> dict[Any, list[int]]:
    """key -> row indices, in first-appearance order (dict insertion order is
    part of the contract: callers rely on it for stable output ordering)."""
    groups: dict[Any, list[int]] = defaultdict(list)
    for i, chunk in enumerate(chunks):
        if predicate is not None and not predicate(chunk):
            continue
        groups[key(chunk)].append(i)
    return dict(groups)


def pooled_vectors(embs: np.ndarray, groups: dict[Any, list[int]]
                   ) -> tuple[list[Any], np.ndarray]:
    """(keys, matrix) of mean-pooled vectors, skipping degenerate groups.
    Key order follows `groups`' insertion order."""
    keys: list[Any] = []
    vectors: list[np.ndarray] = []
    for key, idxs in groups.items():
        v = mean_pool(embs, idxs)
        if v is not None:
            keys.append(key)
            vectors.append(v)
    dim = embs.shape[1] if embs.size else 1
    return keys, (np.vstack(vectors) if vectors else np.zeros((0, dim)))


def blockwise_pairs(embs: np.ndarray, threshold: float,
                    block_size: int = BLOCK_SIZE) -> list[tuple[int, int, float]]:
    """(i, j, score) for every i<j pair with cosine similarity > threshold,
    computed tile-by-tile so peak memory is O(block_size^2).

    Rows must already be L2-normalized (cosine == dot product)."""
    n = embs.shape[0]
    pairs: list[tuple[int, int, float]] = []
    for bi in range(0, n, block_size):
        a = embs[bi:bi + block_size]
        for bj in range(bi, n, block_size):
            b = embs[bj:bj + block_size]
            sim = a @ b.T
            if bi == bj:
                # restrict to i<j by INDEX, not by zeroing-then-thresholding --
                # a zeroed self/lower-triangle entry would itself satisfy a
                # negative threshold (0.0 > -1.0) and leak as a bogus pair.
                li_idx, lj_idx = np.triu_indices(sim.shape[0], k=1)
                scores = sim[li_idx, lj_idx]
                mask = scores > threshold
                for li, lj, score in zip(li_idx[mask], lj_idx[mask], scores[mask], strict=False):
                    pairs.append((bi + int(li), bj + int(lj), float(score)))
            else:
                for li, lj in np.argwhere(sim > threshold):
                    pairs.append((bi + int(li), bj + int(lj), float(sim[li, lj])))
    return pairs


def cross_best_pair(a_embs: np.ndarray, a_idxs: Iterable[int],
                    b_embs: np.ndarray, b_idxs: Iterable[int]
                    ) -> tuple[int | None, int | None, float]:
    """Highest-scoring (a_row, b_row) pair across two chunk sets — the evidence
    behind a whole-note match ("which passage of the source matches which
    section of the note"). Ties break on the lowest index pair so a repeated
    run is byte-stable."""
    a_list, b_list = list(a_idxs), list(b_idxs)
    if not a_list or not b_list:
        return None, None, 0.0
    sim = a_embs[a_list] @ b_embs[b_list].T
    flat = int(np.argmax(sim))
    ai, bi = divmod(flat, sim.shape[1])
    best = float(sim[ai, bi])
    # np.argmax already returns the FIRST maximum in row-major order, which is
    # exactly the lowest (a, b) index pair — no extra tiebreak needed.
    return a_list[ai], b_list[bi], best
