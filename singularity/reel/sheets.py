"""Contact sheets — the only way to actually look at the material.

Two densities, and both are needed for different questions:

  overview (6 frames)  — how do these clips DIFFER? This is where a batch of
                         image-to-video variations gives itself away.
  dense (every 0.5 s)  — where exactly to cut, and which stretches are unusable.
                         Mandatory for AI footage: generation artefacts pile up
                         toward the END of a clip, so a sparse sheet misses them.

`cuts` is the acceptance sheet: the last frame of the outgoing shot next to the
first frame of the incoming one, for every cut. A jump cut, an axis flip and a
backwards step of the emotional ladder are visible there and nowhere else.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import ToolError, run

_FONT = "C\\:/Windows/Fonts/arialbd.ttf"


def _tc(size: int = 34) -> str:
    return (f"drawtext=fontfile='{_FONT}':text='%{{pts\\:hms}}':x=8:y=8:"
            f"fontsize={size}:fontcolor=yellow:box=1:boxcolor=black@0.65")


def _n(size: int = 60) -> str:
    return (f"drawtext=fontfile='{_FONT}':text='%{{n}}':x=10:y=10:"
            f"fontsize={size}:fontcolor=yellow:box=1:boxcolor=black@0.7")


def overview(src: str | Path, out: str | Path, *, cols: int = 3, rows: int = 2,
             tile_w: int = 368) -> Path:
    """`cols*rows` frames spread across the clip."""
    src, out = Path(src), Path(out)
    n = cols * rows
    picks = [f"eq(n\\,{int(i)})" for i in
             (int(12 + i * 60) for i in range(n))]
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vf", f"select='{'+'.join(picks)}',scale={tile_w}:-1,tile={cols}x{rows}",
         "-frames:v", "1", "-fps_mode", "passthrough", str(out)])
    return out


def dense(src: str | Path, out: str | Path, *, fps: float, step_sec: float = 0.5,
          start_sec: float = 0.0, cols: int = 5, rows: int = 5,
          tile_w: int = 276) -> Path:
    """A frame every `step_sec`, each stamped with its timecode.

    `-fps_mode passthrough` is not optional: without it `select` hands ffmpeg a
    re-timed stream and the sheet shows the wrong frames."""
    src, out = Path(src), Path(out)
    step = max(1, int(round(step_sec * fps)))
    first = int(round(start_sec * fps))
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vf", (f"select='not(mod(n-{first},{step}))*gte(n,{first})',{_tc()},"
                 f"scale={tile_w}:-1,tile={cols}x{rows}"),
         "-frames:v", "1", "-fps_mode", "passthrough", str(out)])
    return out


def cuts(src: str | Path, out: str | Path, edges: list[int], *,
         cols: int = 8, tile_w: int = 170, crop: str | None = None) -> Path:
    """Before/after pair at every cut. `edges` = first frame of each new shot."""
    src, out = Path(src), Path(out)
    picked: list[int] = []
    for e in edges:
        if e > 0:
            picked += [e - 1, e]
    if not picked:
        raise ToolError("no interior cuts to sheet")
    rows = (len(picked) + cols - 1) // cols
    sel = "+".join(f"eq(n\\,{f})" for f in picked)
    vf = f"select='{sel}'"
    if crop:
        vf += f",{crop}"
    vf += f",{_n()},scale={tile_w}:-1,tile={cols}x{rows}"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vf", vf, "-frames:v", "1", "-fps_mode", "passthrough", str(out)])
    return out


def strip(src: str | Path, out: str | Path, frames: list[int], *, cols: int = 5,
          tile_w: int = 280, crop: str | None = None, label: bool = True) -> Path:
    """Arbitrary frame list as a grid — used to audit a text or effect layer."""
    src, out = Path(src), Path(out)
    rows = (len(frames) + cols - 1) // cols
    vf = "select='" + "+".join(f"eq(n\\,{f})" for f in frames) + "'"
    if crop:
        vf += f",{crop}"
    if label:
        vf += f",{_n(52)}"
    vf += f",scale={tile_w}:-1,tile={cols}x{rows}"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(src),
         "-vf", vf, "-frames:v", "1", "-fps_mode", "passthrough", str(out)])
    return out


def build_all(files: list[dict[str, Any]], out_dir: str | Path, *,
              step_sec: float = 0.5) -> dict[str, Any]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    made, problems = [], []
    for rec in files:
        stem = Path(rec["name"]).stem[:40]
        try:
            o = overview(rec["path"], out_dir / f"{stem}.overview.jpg")
            d = dense(rec["path"], out_dir / f"{stem}.dense.jpg",
                      fps=rec.get("fps") or 24, step_sec=step_sec)
            made.append({"name": rec["name"], "overview": str(o), "dense": str(d)})
        except Exception as exc:
            problems.append(f"{rec['name']}: {exc}")
    return {"sheets": made, "problems": problems}
