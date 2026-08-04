"""Intake: what did we actually get?

Runs before anything else (vault: `reference/Приёмка и аудит исходников для
монтажа`). Three findings this catches that nothing downstream would:

  * byte-identical duplicates — same size is a hint, MD5 is the proof;
  * clips generated image-to-video from ONE start frame, whose first seconds
    are interchangeable, so a naive concatenation replays the same opening;
  * a loudness spread across clips, which decides whether source audio can be
    cut together at all or the piece needs one continuous bed.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .util import loudness, probe_json

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}
AUDIO_EXT = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}


def _md5(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _rate(text: str | None) -> float:
    if not text:
        return 0.0
    if "/" in text:
        a, b = text.split("/", 1)
        return float(a) / float(b) if float(b) else 0.0
    return float(text)


def inspect(target: str | Path, *, with_loudness: bool = True) -> dict[str, Any]:
    """Inventory of a folder (non-recursive) or a single file."""
    p = Path(target)
    files = sorted([f for f in p.iterdir() if f.is_file()]) if p.is_dir() else [p]
    videos, audios, problems = [], [], []
    subdirs = [d.name for d in p.iterdir() if d.is_dir()] if p.is_dir() else []

    for f in files:
        ext = f.suffix.lower()
        if ext not in VIDEO_EXT and ext not in AUDIO_EXT:
            continue
        try:
            j = probe_json(f)
        except Exception as exc:                    # a broken file is isolated,
            problems.append(f"{f.name}: {exc}")     # never silently skipped
            continue
        fmt = j.get("format", {})
        streams = j.get("streams", [])
        v = next((s for s in streams if s.get("codec_type") == "video"), None)
        a = next((s for s in streams if s.get("codec_type") == "audio"), None)
        rec: dict[str, Any] = {
            "name": f.name, "path": str(f),
            "duration": round(float(fmt.get("duration", 0) or 0), 3),
            "bitrate_kbps": round(float(fmt.get("bit_rate", 0) or 0) / 1000),
            "md5": _md5(f),
        }
        if v is not None:
            fps = _rate(v.get("r_frame_rate"))
            rec.update({"width": v.get("width"), "height": v.get("height"),
                        "fps": round(fps, 4), "vcodec": v.get("codec_name"),
                        "frames": int(round(rec["duration"] * fps)) if fps else 0,
                        "rotation": int((v.get("side_data_list") or [{}])[0].get("rotation", 0) or 0)})
        if a is not None:
            rec.update({"acodec": a.get("codec_name"),
                        "sample_rate": int(a.get("sample_rate") or 0),
                        "channels": a.get("channels")})
        if with_loudness and (a is not None or ext in AUDIO_EXT):
            rec["loudness"] = loudness(f)
        (audios if (ext in AUDIO_EXT or v is None) else videos).append(rec)

    by_hash: dict[str, list[str]] = {}
    for rec in videos + audios:
        by_hash.setdefault(rec["md5"], []).append(rec["name"])
    dupes = {h: names for h, names in by_hash.items() if len(names) > 1}
    for rec in videos + audios:
        rec["duplicate_of"] = next(
            (names[0] for h, names in dupes.items()
             if rec["md5"] == h and rec["name"] != names[0]), None)

    unique = [r for r in videos if r.get("duplicate_of") is None]
    warnings: list[str] = []
    if subdirs:
        warnings.append(f"subdirectories not scanned: {', '.join(sorted(subdirs))}")
    if dupes:
        warnings.append(f"{sum(len(v) - 1 for v in dupes.values())} byte-identical duplicate(s) "
                        "— dropped from the unique set")
    if unique:
        sizes = {(r.get("width"), r.get("height")) for r in unique}
        if len(sizes) > 1:
            warnings.append(f"mixed frame sizes: {sorted(sizes, key=str)}")
        rates = {r.get("fps") for r in unique}
        if len(rates) > 1:
            warnings.append(f"mixed frame rates: {sorted(rates)} — conform before cutting")
        durs = {round(r["duration"], 1) for r in unique}
        if len(durs) == 1 and len(unique) > 2:
            warnings.append("all clips are the same length — likely one generation batch; "
                            "check whether they share a start frame (reel sheets)")
        louds = [r["loudness"]["lufs"] for r in unique
                 if r.get("loudness", {}).get("lufs") is not None]
        if louds and max(louds) - min(louds) > 3.0:
            warnings.append(f"loudness spread {max(louds) - min(louds):.1f} LU across clips — "
                            "source audio will not cut together; plan one continuous bed")

    return {"root": str(p), "videos": videos, "audio": audios,
            "unique_videos": [r["name"] for r in unique],
            "duplicates": dupes, "warnings": warnings, "problems": problems}
