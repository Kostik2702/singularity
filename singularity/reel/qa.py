"""Acceptance gates that answer with numbers.

Three things a reel must survive before it ships, none of which the eye judges
reliably:

  flash_check   WCAG 2.3.1 — no more than 3 flashes per second. This is the one
                gate in the pipeline with a medical rather than an aesthetic
                boundary (vault: `reference/Вспышки и фотосенситивность
                в short-form`).
  band_contrast the worst background under each text row across EVERY frame the
                copy is on screen, and the resulting WCAG ratio.
  audio_check   integrated loudness, true peak and sample rate of the master.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import frames, loudness, probe_json


def _srgb_to_lin(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    r, g, b = (_srgb_to_lin(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(fg: tuple[float, float, float], bg: tuple[float, float, float]) -> float:
    a, b = relative_luminance(fg), relative_luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def frame_luminance(path: str | Path, w: int, h: int) -> list[float]:
    """Mean relative luminance per frame, linearised. Streamed, never buffered."""
    import numpy as np
    lut = np.array([_srgb_to_lin(v) for v in range(256)], dtype=np.float32)
    out: list[float] = []
    for buf in frames(path, w, h, "rgb24"):
        a = np.frombuffer(buf, dtype=np.uint8).reshape(-1, 3)
        lin = lut[a]
        out.append(float((0.2126 * lin[:, 0] + 0.7152 * lin[:, 1]
                          + 0.0722 * lin[:, 2]).mean()))
    return out


def flash_check(path: str | Path, w: int, h: int, fps: int = 24,
                window: int = 6, threshold: float = 0.10) -> dict[str, Any]:
    """WCAG 2.3.1 general-flash count.

    Peaks are found against WINDOWED extrema (+-window frames), never by a
    monotonic walk outward: real footage is noisy, one frame ticking upward
    truncates the descent, and the check then reports a comfortable PASS on a
    reel that actually violates the threshold. A green verdict from a lenient
    detector is worse than no check at all."""
    import numpy as np
    lum = np.asarray(frame_luminance(path, w, h), dtype=np.float64)
    if lum.size == 0:
        return {"ok": False, "problems": ["no frames decoded"]}

    flashes = []
    for i in range(lum.size):
        lo_i, hi_i = max(0, i - window), min(lum.size, i + window + 1)
        if lum[i] < lum[lo_i:hi_i].max():
            continue
        rise = lum[i] - lum[lo_i:i + 1].min()
        fall = lum[i] - lum[i:hi_i].min()
        darker = min(lum[lo_i:i + 1].min(), lum[i:hi_i].min())
        if rise >= threshold and fall >= threshold and darker < 0.80:
            flashes.append({"frame": int(i), "luminance": round(float(lum[i]), 4),
                            "rise": round(float(rise), 4), "fall": round(float(fall), 4)})

    worst = 0
    for start in range(0, lum.size):
        worst = max(worst, sum(1 for f in flashes if start <= f["frame"] < start + fps))

    d = np.abs(np.diff(lum))
    top = np.argsort(d)[-5:][::-1] if d.size else []
    return {"ok": worst <= 3, "flashes": flashes, "max_per_second": worst, "limit": 3,
            "luminance": {"min": round(float(lum.min()), 4),
                          "mean": round(float(lum.mean()), 4),
                          "max": round(float(lum.max()), 4)},
            "largest_steps": [{"from": int(t), "to": int(t) + 1,
                               "delta": round(float(d[t]), 4)} for t in top],
            "problems": [] if worst <= 3
                        else [f"{worst} flashes in one second, WCAG limit is 3"]}


def band_contrast(path: str | Path, w: int, h: int, bands: list[dict[str, Any]],
                  windows: list[tuple[int, int]] | None = None) -> dict[str, Any]:
    """Worst background under each text band, over every frame it is visible.

    `bands` = [{"name": "row1", "y0":…, "y1":…, "x0":…, "x1":…, "rgb": [r,g,b]}]
    `windows` = frame ranges the copy is on screen; None scans everything."""
    import numpy as np
    inwin = (lambda f: True) if not windows else \
        (lambda f: any(a <= f < b for a, b in windows))
    worst = {b["name"]: (0.0, -1) for b in bands}
    for i, buf in enumerate(frames(path, w, h, "gray")):
        if not inwin(i):
            continue
        a = np.frombuffer(buf, dtype=np.uint8).reshape(h, w)
        for b in bands:
            m = float(a[b["y0"]:b["y1"], b.get("x0", 0):b.get("x1", w)].mean())
            if m > worst[b["name"]][0]:
                worst[b["name"]] = (m, i)
    rows = []
    for b in bands:
        bg, frame = worst[b["name"]]
        fg = tuple(b.get("rgb", (255, 255, 255)))
        ratio = contrast_ratio(fg, (bg, bg, bg))
        rows.append({"band": b["name"], "worst_frame": frame,
                     "background": round(bg, 1), "fg": list(fg),
                     "ratio": round(ratio, 2),
                     "passes_large_text": ratio >= 3.0,
                     "passes_normal_text": ratio >= 4.5})
    return {"ok": all(r["passes_large_text"] for r in rows), "bands": rows,
            "problems": [f"{r['band']}: {r['ratio']}:1 is below the 3:1 large-text floor"
                         for r in rows if not r["passes_large_text"]]}


def audio_check(path: str | Path, target_lufs: float = -14.0,
                tol: float = 1.0) -> dict[str, Any]:
    j = probe_json(path)
    a = next((s for s in j.get("streams", []) if s.get("codec_type") == "audio"), None)
    loud = loudness(path)
    problems = []
    if a is None:
        problems.append("no audio stream")
    else:
        sr = int(a.get("sample_rate") or 0)
        if sr != 48000:
            problems.append(f"sample rate {sr} Hz — expected 48000; a loudnorm chain "
                            "without aresample/-ar lands on 96 or 192 kHz")
    if loud.get("lufs") is not None and abs(loud["lufs"] - target_lufs) > tol:
        problems.append(f"{loud['lufs']} LUFS is more than {tol} LU off the {target_lufs} target")
    if loud.get("true_peak") is not None and loud["true_peak"] > -1.0:
        problems.append(f"true peak {loud['true_peak']} dBFS leaves no headroom for re-encoding")
    return {"ok": not problems, "loudness": loud,
            "sample_rate": int(a.get("sample_rate") or 0) if a else 0,
            "problems": problems}


def master_check(path: str | Path, *, target_lufs: float = -14.0,
                 flash: bool = True) -> dict[str, Any]:
    j = probe_json(path)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    if v is None:
        return {"ok": False, "problems": ["no video stream"]}
    w, h = int(v["width"]), int(v["height"])
    fps_txt = v.get("r_frame_rate", "24/1")
    num, den = (fps_txt.split("/") + ["1"])[:2]
    fps = int(round(float(num) / float(den or 1)))
    rep: dict[str, Any] = {"path": str(path), "size": [w, h], "fps": fps,
                           "frames": int(v.get("nb_frames") or 0),
                           "duration": round(float(j["format"].get("duration", 0) or 0), 3)}
    rep["audio"] = audio_check(path, target_lufs)
    if flash:
        rep["flash"] = flash_check(path, w, h, fps)
    problems = list(rep["audio"]["problems"]) + list(rep.get("flash", {}).get("problems", []))
    rep["ok"] = not problems
    rep["problems"] = problems
    return rep
