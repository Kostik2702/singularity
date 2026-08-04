"""Light hits and colour grades.

Stage lights are an ARC that follows the montage acts, never a uniform strobe —
the anti-pattern checklist requires every flash to have a function and a timing,
and a constant strobe has neither.

Safety is structural, not advisory. Amplitude is spent where the frequency
margin is biggest: an on-BAR hit fires ~0.5 times a second and may be hard, an
on-beat pulse fires ~2 times a second and stays low. Nothing is ever allowed on
a beat SUBDIVISION — that would cross 3 flashes/second and the WCAG 2.3.1
boundary. `LightRig.verify_cadence` refuses a configuration that would.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .edl import Grid

GRADES: dict[str, str] = {
    "none": "",
    "neutral": "eq=contrast=1.04:saturation=1.02",
    "club": "eq=contrast=1.06:saturation=1.10",
    "warm-film": "eq=contrast=1.06:saturation=0.97:gamma_r=1.03:gamma_b=0.975",
    "cool-night": "eq=contrast=1.05:saturation=0.95:gamma_b=1.04:gamma_r=0.98",
    "bleach": "eq=contrast=1.12:saturation=0.72",
}


@dataclass
class LightAct:
    """Amplitude for one stretch of the reel, in cumulative beats."""
    from_beat: float
    to_beat: float
    on_beat: float = 0.0
    on_bar: float = 0.0
    taper: bool = False        # fade the act's own hits to zero across it


@dataclass
class LightRig:
    grid: Grid
    acts: list[LightAct]
    colours: list[tuple[int, int, int]] = field(
        default_factory=lambda: [(255, 46, 136), (60, 200, 255)])
    decay: float = 9.0             # ~2-3 frames: a hit, not a colour wash
    bar_white: float = 0.35        # bar hits lean toward a strobe head
    grad_floor: float = 0.25       # light falls from above, never flat
    _veils: dict = field(default_factory=dict, repr=False)

    # -- safety ---------------------------------------------------------------
    def verify_cadence(self) -> list[str]:
        problems = []
        per_sec_beat = self.grid.bpm / 60.0
        per_sec_bar = per_sec_beat / self.grid.beats_per_bar
        for a in self.acts:
            if a.on_beat > 0 and per_sec_beat > 3.0:
                problems.append(
                    f"act {a.from_beat}-{a.to_beat}: an on-beat hit at {self.grid.bpm} BPM "
                    f"fires {per_sec_beat:.2f}/s, above the WCAG 3/s ceiling")
            if a.on_bar > 0 and per_sec_bar > 3.0:
                problems.append(
                    f"act {a.from_beat}-{a.to_beat}: an on-bar hit fires {per_sec_bar:.2f}/s")
        return problems

    # -- per-frame ------------------------------------------------------------
    def amount(self, frame: int) -> tuple[float, tuple[int, int, int] | None]:
        bi = self.grid.beat_of_frame(frame)
        frac = self.grid.frac_into_beat(frame)
        on_bar = bi % self.grid.beats_per_bar == 0
        amp = 0.0
        for a in self.acts:
            if a.from_beat <= bi < a.to_beat:
                amp = a.on_bar if on_bar else a.on_beat
                if a.taper and a.to_beat > a.from_beat:
                    amp *= max(0.0, (a.to_beat - bi) / (a.to_beat - a.from_beat))
                break
        if amp <= 0.001:
            return 0.0, None
        col = self.colours[(bi // self.grid.beats_per_bar) % len(self.colours)]
        if on_bar:
            col = tuple(int(c + (255 - c) * self.bar_white) for c in col)
        return amp * math.exp(-frac * self.decay), col

    def _veil(self, colour: tuple[int, int, int], size: tuple[int, int]):
        from PIL import Image, ImageChops
        key = (colour, size)
        if key not in self._veils:
            w, h = size
            g = Image.new("L", (1, h))
            px = g.load()
            for y in range(h):
                px[0, y] = int(255 * (1.0 - (1.0 - self.grad_floor) * (y / float(h)) ** 0.85))
            grad = g.resize((w, h))
            base = Image.new("RGB", (w, h), colour)
            self._veils[key] = ImageChops.multiply(base, Image.merge("RGB", (grad, grad, grad)))
        return self._veils[key]

    def apply(self, frame, f: int):
        """Screen the veil over the FOOTAGE. Call this BEFORE compositing copy —
        graphics must sit above the light or their measured contrast stops
        holding."""
        from PIL import ImageChops
        amt, col = self.amount(f)
        if amt <= 0.004 or col is None:
            return frame
        veil = self._veil(col, frame.size).point(lambda v, k=amt: int(v * k))
        return ImageChops.screen(frame.convert("RGB"), veil).convert("RGBA")

    def hits(self, total_frames: int) -> list[dict[str, Any]]:
        """Every beat position where the rig actually fires, with its peak amount.

        This is the list a sound pass needs: a flash whose click arrives on a
        different frame reads as two events, not one. Producing it HERE, from the
        same `amount()` the picture pass used, is what keeps them in lockstep —
        recomputing the cadence on the audio side is how they drift apart."""
        out: list[dict[str, Any]] = []
        b = 0
        while True:
            f = self.grid.frame_of_beat(b)
            if f >= total_frames:
                break
            if f >= 0:
                amt, _ = self.amount(f)
                if amt > 0.01:
                    out.append({"beat": b, "frame": f,
                                "t": round(f / self.grid.fps, 4),
                                "amp": round(amt, 4),
                                "bar": b % self.grid.beats_per_bar == 0})
            b += 1
        return out

    def report(self) -> dict[str, Any]:
        rows = []
        for a in self.acts:
            b = int(a.from_beat)
            while b < a.to_beat:
                if b % self.grid.beats_per_bar == 0:
                    amt, col = self.amount(self.grid.frame_of_beat(b))
                    rows.append({"beat": b, "bar": b // self.grid.beats_per_bar + 1,
                                 "frame": self.grid.frame_of_beat(b),
                                 "amp": round(amt, 3), "colour": list(col) if col else None})
                b += 1
        return {"bars": rows, "cadence_problems": self.verify_cadence()}


def act_arc(total_beats: float, *, peak_from: float = 0.55, peak_to: float = 0.85,
            beats_per_bar: int = 4) -> list[LightAct]:
    """A default four-act arc: establish on bars only, add the beat pulse, peak,
    then taper to nothing so the closing shot stays clean."""
    a = total_beats * 0.28
    b = total_beats * peak_from
    c = total_beats * peak_to
    return [
        LightAct(0, a, on_beat=0.00, on_bar=0.16),
        LightAct(a, b, on_beat=0.08, on_bar=0.20),
        LightAct(b, c, on_beat=0.12, on_bar=0.36),
        LightAct(c, total_beats, on_beat=0.00, on_bar=0.22, taper=True),
    ]


def camera_arc(total_beats: float, *, bar_amp: float = 0.85, beat_amp: float = 0.30,
               beats_per_bar: int = 4) -> list[LightAct]:
    """A photographer's arc rather than a lighting desk's.

    The shape is the same four acts, but the reasoning differs: a photographer
    starts with single frames, joins the motordrive in as the scene escalates,
    and — this is the part that matters — STOPS over the closing shot. A flash
    across the last beat interrupts the one image the reel exists to deliver, so
    act IV tapers to nothing on the beat and keeps only a dying bar hit."""
    a = total_beats * 0.28
    b = total_beats * 0.55
    c = total_beats * 0.88
    return [
        LightAct(0, a, on_beat=0.00, on_bar=bar_amp * 0.65),
        LightAct(a, b, on_beat=beat_amp * 0.55, on_bar=bar_amp * 0.80),
        LightAct(b, c, on_beat=beat_amp, on_bar=bar_amp),
        LightAct(c, total_beats, on_beat=0.00, on_bar=bar_amp * 0.55, taper=True),
    ]


def camera_rig(grid: Grid, total_beats: float, *, bar_amp: float = 0.85,
               beat_amp: float = 0.30, decay: float = 13.0) -> LightRig:
    """A xenon flash, not a stage wash.

    Three parameters carry the difference and none of them is the amplitude:
    the colour is daylight-white with a hair of warmth (a coloured hit reads as
    a gel, i.e. as a lighting desk); the decay is fast enough to live inside one
    or two frames (a flash that lingers is a lamp); and the gradient is nearly
    flat, because the photographer stands in FRONT of the subject while stage
    light falls from above."""
    return LightRig(grid=grid,
                    acts=camera_arc(total_beats, bar_amp=bar_amp, beat_amp=beat_amp,
                                    beats_per_bar=grid.beats_per_bar),
                    colours=[(255, 253, 247)],
                    decay=decay,
                    bar_white=0.0,
                    grad_floor=0.72)


def apply_rig(master, out, rig: LightRig, *, total_frames: int,
              canvas: tuple[int, int], bed=None, crf: int = 16,
              preset: str = "slow") -> dict[str, Any]:
    """Stream a finished master through a light rig.

    `verify_cadence` is a GATE here, not a warning: a configuration that would
    cross the WCAG 2.3.1 ceiling refuses to render at all. The threshold is
    medical, so the caller does not get to weigh it against taste."""
    import subprocess

    from PIL import Image

    from .util import ToolError

    problems = rig.verify_cadence()
    if problems:
        raise ToolError("; ".join(problems))
    w, h = canvas
    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(master),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pixel_format", "rgb24",
           "-video_size", f"{w}x{h}", "-framerate", str(rig.grid.fps), "-i", "pipe:0"]
    if bed:
        cmd += ["-i", str(bed), "-map", "0:v", "-map", "1:a",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(out)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    written = 0
    try:
        for f in range(total_frames):
            buf = dec.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            frame = Image.frombytes("RGB", (w, h), buf).convert("RGBA")
            enc.stdin.write(rig.apply(frame, f).convert("RGB").tobytes())
            written += 1
    finally:
        try:
            enc.stdin.close()
        except Exception:
            pass
        if dec.poll() is None:
            dec.kill()
        dec.wait()
    if enc.wait():
        raise ToolError("light pass encode failed")
    hits = rig.hits(written)
    return {"out": str(out), "frames": written, "hits": hits,
            "fired": len(hits), "per_second": round(len(hits) / max(1e-6, written / rig.grid.fps), 3)}
