"""Tempo, downbeat, section map and loop-seam checking.

`detect_beats` and `energy_map` are the two questions worth asking of a track
before cutting anything: *where is the grid* and *which 30-40 seconds of this
should the reel live in*. Both answer with numbers, so the choice is arguable
rather than a matter of taste.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .util import pcm_mono


@dataclass
class Tempo:
    bpm: float
    phase: float                 # seconds to the first downbeat
    confidence: float            # 2nd-best autocorrelation peak / best
    candidates: list[tuple[float, float]]

    @property
    def beat(self) -> float:
        return 60.0 / self.bpm


def detect_beats(path: str | Path, sr: int = 22050,
                 bpm_range: tuple[float, float] = (60.0, 190.0),
                 beats_per_bar: int = 4) -> Tempo:
    """Spectral-flux onset envelope -> autocorrelation -> phase search.

    No external MIR dependency: numpy and ffmpeg only. A flat top of the
    candidate list (0.99 of the best over several neighbouring BPM) means the
    tempo is steady, not that the detector is unsure — read `confidence` that
    way round."""
    import numpy as np

    x = pcm_mono(path, sr)
    if x.size < sr:
        raise ValueError("track shorter than a second")

    hop, win = 256, 1024
    n = (len(x) - win) // hop
    if n < 8:
        raise ValueError("track too short for tempo detection")
    w = np.hanning(win).astype(np.float32)
    S = np.empty((n, win // 2 + 1), np.float32)
    for i in range(n):
        S[i] = np.abs(np.fft.rfft(x[i * hop:i * hop + win] * w))
    flux = np.maximum(0.0, np.diff(S, axis=0)).sum(axis=1)
    flux -= flux.mean()
    fps = sr / hop

    ac = np.correlate(flux, flux, mode="full")[len(flux) - 1:]
    scored: list[tuple[float, float]] = []
    bpm = bpm_range[0]
    while bpm <= bpm_range[1]:
        lag = 60.0 / bpm * fps
        lo, hi = int(math.floor(lag)), int(math.ceil(lag))
        if hi < len(ac):
            frac = lag - lo
            scored.append((float(ac[lo] * (1 - frac) + ac[hi] * frac), bpm))
        bpm += 0.25
    scored.sort(reverse=True)
    best_v, best_bpm = scored[0]
    period = 60.0 / best_bpm

    sec = np.arange(len(flux)) / fps
    phase, best_score = 0.0, -math.inf
    step = 0.005
    ph = 0.0
    while ph < period:
        idx = np.round(np.arange(ph, sec[-1], period) * fps).astype(int)
        idx = idx[(idx >= 0) & (idx < len(flux))]
        score = float(flux[idx].sum())
        if score > best_score:
            best_score, phase = score, ph
        ph += step

    # phase found on the beat grid; fold it onto the BAR grid by picking the
    # downbeat candidate with the strongest onset sum
    bar = period * beats_per_bar
    best_bar, bar_score = phase, -math.inf
    for k in range(beats_per_bar):
        cand = phase + k * period
        idx = np.round(np.arange(cand, sec[-1], bar) * fps).astype(int)
        idx = idx[(idx >= 0) & (idx < len(flux))]
        score = float(flux[idx].sum())
        if score > bar_score:
            bar_score, best_bar = score, cand

    conf = float(scored[1][0] / best_v) if len(scored) > 1 and best_v else 0.0
    return Tempo(bpm=round(best_bpm, 3), phase=round(best_bar, 4), confidence=round(conf, 4),
                 candidates=[(round(b, 2), round(v / best_v, 3)) for v, b in scored[:8]])


def energy_map(path: str | Path, bpm: float, phase: float, sr: int = 22050,
               beats_per_bar: int = 4) -> list[dict[str, Any]]:
    """Per-bar RMS and 'brightness' (share of energy above 2 kHz).

    Brightness is what exposes structure: a chorus does not just get louder, it
    gets brighter, and a section change shows up as a step in this column even
    when RMS barely moves."""
    import numpy as np

    x = pcm_mono(path, sr)
    bar = 60.0 / bpm * beats_per_bar
    out: list[dict[str, Any]] = []
    b = 0
    while True:
        t0 = phase + b * bar
        seg = x[int(t0 * sr):int((t0 + bar) * sr)]
        if len(seg) < sr // 8:
            break
        S = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        fr = np.fft.rfftfreq(len(seg), 1.0 / sr)
        total = float(S.sum()) or 1e-9
        out.append({"bar": b + 1, "t": round(float(t0), 3),
                    "rms": round(float(np.sqrt((seg ** 2).mean())), 5),
                    "bright": round(float(S[fr > 2000].sum() / total), 4)})
        b += 1
    return out


def suggest_section(bars: list[dict[str, Any]], want_bars: int) -> dict[str, Any]:
    """Pick a window that climbs into the peak and lands after it.

    Scores every candidate window on: how much of the track's peak energy it
    contains, that it does not START at the loudest point (a reel that opens at
    maximum has nowhere to go), and that it ends at or after the energy turns
    down (so the piece resolves instead of being cut off)."""
    if len(bars) < want_bars:
        return {"start_bar": 1, "t": bars[0]["t"] if bars else 0.0,
                "why": "track shorter than the requested window"}
    rms = [b["rms"] for b in bars]
    peak = max(rms)
    best, best_score = 0, -math.inf
    for i in range(0, len(bars) - want_bars + 1):
        win = rms[i:i + want_bars]
        head = sum(win[:max(1, want_bars // 4)]) / max(1, want_bars // 4)
        tail = sum(win[-max(1, want_bars // 4):]) / max(1, want_bars // 4)
        score = (sum(win) / len(win) / peak) * 2.0        # be near the peak
        score += (max(win) - head) / peak                 # room to grow at the front
        score += 0.6 * (max(win) - tail) / peak           # resolve at the back
        if score > best_score:
            best_score, best = score, i
    b0 = bars[best]
    return {"start_bar": b0["bar"], "t": b0["t"], "bars": want_bars,
            "score": round(best_score, 3),
            "why": "climbs into the peak and lands after it; start is on a downbeat"}


def seam_report(path: str | Path, seams: list[float], sr: int = 48000) -> list[dict[str, Any]]:
    """Is a butt-joined loop audible? Compare the sample step at the seam with
    the track's own 99.99th percentile step, and the 100 ms RMS either side.
    Rule of thumb: ratio > ~2 or an RMS step > 1 dB needs a short crossfade."""
    import numpy as np

    x = pcm_mono(path, sr)
    d = np.abs(np.diff(x))
    p9999 = float(np.percentile(d, 99.99)) or 1e-9
    out = []
    for s in seams:
        i = int(s * sr)
        if i <= 5 or i >= len(x) - 5:
            continue
        step = float(d[max(0, i - 5):i + 5].max())
        a = float(np.sqrt((x[max(0, i - sr // 10):i] ** 2).mean()))
        b = float(np.sqrt((x[i:i + sr // 10] ** 2).mean()))
        out.append({"t": round(s, 4), "step": round(step, 5),
                    "ratio_to_p9999": round(step / p9999, 3),
                    "rms_step_db": round(20 * math.log10((b + 1e-9) / (a + 1e-9)), 3),
                    "needs_crossfade": step / p9999 > 2.0 or abs(20 * math.log10((b + 1e-9) / (a + 1e-9))) > 1.0})
    return out
