"""Animated copy over a master — one engine, several registers.

The method is constant; the grammar changes with the piece. Same pipeline
produced a 126 BPM club promo (condensed caps, white + magenta, glyphs
condensing out of the room's own light, halo breathing on every beat) and a
105.5 BPM love story (serif, sentence case, cream + champagne italic, lines
resolving out of soft focus, no pulse at all). Both are `Register` presets.

Non-negotiables baked in, each learned from a real failure:
  * one type size for the whole reel, fitted to the LONGEST line;
  * a glow the same hue as the letters is decoration, not legibility — on a
    bright background it only smears a same-coloured blob, so the club register
    carries a hard dark contour and the contrast comes from the glyph edge;
  * `font.getlength` does not know about `stroke_width`, so the width budget is
    reduced BEFORE the size is fitted;
  * copy must be gone before the master's fade to black, or it hangs over it.
"""
from __future__ import annotations

import math
import random
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .edl import Grid
from .fonts import EMOJI_FONT, fit_size, line_width
from .util import ToolError

eo = lambda p: 1 - (1 - p) ** 3          # noqa: E731  ease-out cubic
ei = lambda p: p ** 3                    # noqa: E731  ease-in cubic
clamp = lambda v: 0.0 if v < 0 else (1.0 if v > 1 else v)   # noqa: E731


@dataclass
class Register:
    """A complete typographic + motion grammar."""
    name: str
    font_roman: str
    font_italic: str | None = None
    weight_roman: int | None = None
    weight_italic: int | None = None
    upper: bool = False
    track: float = 2.0
    row1_rgb: tuple[int, int, int] = (255, 255, 255)
    row2_rgb: tuple[int, int, int] = (255, 255, 255)
    row2_italic: bool = False
    stroke: int = 0                       # hard dark contour, px
    shadow_alpha: int = 190
    shadow_blur: int = 10
    glow: bool = False                    # bloom on row 2
    scrim: int = 0                        # 0 = none; peak alpha of the plate
    entry: str = "rise"                   # rise | light | focus
    entry_frames: int = 6
    row2_lag: int = 6
    rise: int = 26
    exit_frames: int = 6
    drift: int = 14
    pulse: float = 0.0                    # halo amplitude on each beat
    kicker: int = 0                       # width of the accent rule, 0 = none
    leading: float = 1.16
    blur_in: float = 0.0                  # focus register
    scatter: tuple[int, int] = (40, 115)  # light register
    max_width: int = 912
    block_bottom: int = 1420

    def __post_init__(self) -> None:
        # A register loaded from JSON arrives with colours as lists; the drawing
        # code builds RGBA by concatenating an alpha tuple, which silently needs
        # tuples. Normalise once here rather than defensively at every use.
        self.row1_rgb = tuple(self.row1_rgb)
        self.row2_rgb = tuple(self.row2_rgb)
        if self.scatter is not None:
            self.scatter = tuple(self.scatter)


def club_register(font_roman: str, **kw: Any) -> Register:
    return Register(name="club", font_roman=font_roman, upper=True, track=6.0,
                    row1_rgb=(255, 255, 255), row2_rgb=(255, 46, 136),
                    stroke=3, glow=True, scrim=155, entry="light",
                    entry_frames=7, row2_lag=5, pulse=0.34, kicker=230,
                    blur_in=11.0, **kw)


def editorial_register(font_roman: str, font_italic: str, **kw: Any) -> Register:
    return Register(name="editorial", font_roman=font_roman, font_italic=font_italic,
                    weight_roman=600, weight_italic=500, upper=False, track=2.0,
                    row1_rgb=(250, 245, 238), row2_rgb=(226, 186, 130),
                    row2_italic=True, stroke=0, shadow_alpha=185, shadow_blur=12,
                    glow=False, scrim=0, entry="focus", entry_frames=14,
                    row2_lag=7, rise=22, exit_frames=12, drift=12,
                    pulse=0.0, kicker=0, leading=1.30, blur_in=9.0,
                    max_width=900, block_bottom=1450, **kw)


@dataclass
class Line:
    row1: str
    row2: str = ""
    in_beat: float = 0.0
    out_beat: float = 0.0


@dataclass
class TextPlan:
    grid: Grid
    register: Register
    lines: list[Line]
    canvas: tuple[int, int] = (1080, 1920)
    tail_stop: int | None = None          # last frame copy may occupy
    size: int = field(default=0, init=False)


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError as exc:                     # pragma: no cover
        raise ToolError("Pillow is required for the text layer") from exc
    return Image, ImageDraw, ImageFilter, ImageFont


def _load(path: str, size: int, weight: int | None):
    _, _, _, ImageFont = _pil()
    f = ImageFont.truetype(path, size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except Exception:
            pass
    return f


def _split_emoji(text: str) -> tuple[str | None, str]:
    if text and ord(text[0]) > 0xFFFF:
        return text[0], text[1:].lstrip()
    return None, text


def _rows(plan: TextPlan) -> Iterable[tuple[str, bool]]:
    for ln in plan.lines:
        yield ln.row1, False
        if ln.row2:
            yield ln.row2, True


def fit(plan: TextPlan) -> int:
    """One size for every line, both styles, with room for the contour."""
    reg = plan.register
    budget = reg.max_width - 2 * reg.stroke
    texts = [t.upper() if reg.upper else t for t, _ in _rows(plan)]
    size = fit_size(reg.font_roman, texts, budget,
                    weight=reg.weight_roman, track=reg.track)
    if reg.font_italic:
        size = min(size, fit_size(reg.font_italic,
                                  [t.upper() if reg.upper else t
                                   for t, ital in _rows(plan) if ital] or texts,
                                  budget, weight=reg.weight_italic, track=reg.track))
    plan.size = size
    return size


def _row_width(text: str, font, efont, track: float) -> float:
    emo, rest = _split_emoji(text)
    w = line_width(rest, font, track)
    if emo and efont:
        w += efont.size * 1.15 + track * 2
    return w


def _draw_row(text: str, font, efont, colour, reg: Register, canvas_w: int):
    Image, ImageDraw, ImageFilter, _ = _pil()
    img = Image.new("RGBA", (canvas_w, int(font.size * 2.1)), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    emo, rest = _split_emoji(text)
    x = (canvas_w - _row_width(text, font, efont, reg.track)) / 2
    y = font.size * 0.30
    if emo and efont:
        d.text((x, y + font.size * 0.06), emo, font=efont, embedded_color=True)
        x += efont.size * 1.15 + reg.track * 2
    for c in rest:
        if reg.stroke:
            d.text((x, y), c, font=font, fill=colour + (255,),
                   stroke_width=reg.stroke, stroke_fill=(0, 0, 0, 255))
        else:
            d.text((x, y), c, font=font, fill=colour + (255,))
        x += font.getlength(c) + reg.track

    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    sh.paste((0, 0, 0, reg.shadow_alpha), (0, 0), img.getchannel("A"))
    sh = sh.filter(ImageFilter.GaussianBlur(reg.shadow_blur))
    out = Image.alpha_composite(sh, sh)
    if reg.glow and colour == reg.row2_rgb:
        bl = img.filter(ImageFilter.GaussianBlur(15))
        bl = Image.merge("RGBA", bl.split()[:3] +
                         (bl.getchannel("A").point(lambda a: min(255, int(a * 1.9))),))
        out = Image.alpha_composite(out, bl)
    return Image.alpha_composite(out, img)


def _scrim(canvas: tuple[int, int], peak: int, bottom: int):
    Image, _, _, _ = _pil()
    w, h = canvas
    g = Image.new("L", (1, h), 0)
    px = g.load()
    top, hold, tail = bottom - 380, bottom - 170, bottom + 260
    for y in range(h):
        if y < top or y > tail:
            a = 0
        elif y < hold:
            a = peak * (y - top) / max(1, hold - top)
        elif y < bottom + 80:
            a = peak
        else:
            a = peak * (tail - y) / max(1, tail - bottom - 80)
        px[0, y] = int(max(0, a))
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    img.putalpha(g.resize((w, h)))
    return img


def _alpha(img, f: float):
    Image, _, _, _ = _pil()
    if f >= 0.999:
        return img
    if f <= 0.004:
        return None
    return Image.merge("RGBA", img.split()[:3] +
                       (img.getchannel("A").point(lambda a: int(a * f)),))


def compile_plan(plan: TextPlan) -> dict[str, Any]:
    """Pre-render every static asset once; per-frame work is compositing only."""
    Image, _, ImageFilter, _ = _pil()
    reg = plan.register
    size = plan.size or fit(plan)
    w, h = plan.canvas
    froman = _load(reg.font_roman, size, reg.weight_roman)
    fital = _load(reg.font_italic, size, reg.weight_italic) if reg.font_italic else froman
    efont = None
    if Path(EMOJI_FONT).exists():
        try:
            _, _, _, ImageFont = _pil()
            efont = ImageFont.truetype(EMOJI_FONT, int(size * 0.95))
        except Exception:
            efont = None

    leading = int(size * reg.leading)
    rnd = random.Random(20260731)          # fixed seed: renders are reproducible
    scrim = _scrim(plan.canvas, reg.scrim, reg.block_bottom) if reg.scrim else None
    built = []
    for ln in plan.lines:
        t1 = ln.row1.upper() if reg.upper else ln.row1
        t2 = (ln.row2.upper() if reg.upper else ln.row2) if ln.row2 else ""
        f2 = fital if (reg.row2_italic and reg.font_italic) else froman
        s1 = _draw_row(t1, froman, efont, reg.row1_rgb, reg, w)
        s2 = _draw_row(t2, f2, efont, reg.row2_rgb, reg, w) if t2 else None
        anchor = s2 or s1
        y2 = reg.block_bottom - anchor.height + int(size * 0.30)
        y1 = y2 - leading if s2 else y2
        rows = [{"img": s1, "y": y1, "lag": 0}]
        if s2:
            rows.append({"img": s2, "y": y2, "lag": reg.row2_lag})
        halos = []
        for r in rows:
            a = r["img"].getchannel("A").filter(ImageFilter.GaussianBlur(22))
            colour = reg.row2_rgb if r is rows[-1] and s2 else reg.row1_rgb
            halo = Image.new("RGBA", r["img"].size, colour + (0,))
            halo.putalpha(a.point(lambda v: min(255, int(v * 1.5))))
            halos.append(halo if reg.pulse > 0 else None)
        for r, halo in zip(rows, halos):
            r["halo"] = halo
            if reg.entry == "light":
                r["glyphs"] = _glyph_sprites(r["img"], rnd, reg)
        f_out = plan.grid.frame_of_beat(ln.out_beat)
        if plan.tail_stop is not None:
            f_out = min(f_out, plan.tail_stop)
        built.append({"rows": rows, "f_in": plan.grid.frame_of_beat(ln.in_beat),
                      "f_out": f_out,
                      "kicker": Image.new("RGBA", (reg.kicker, 4), reg.row2_rgb + (255,))
                      if reg.kicker else None,
                      "y_kicker": y1 - 32})
    return {"size": size, "scrim": scrim, "lines": built}


def _glyph_sprites(row_img, rnd: random.Random, reg: Register):
    """Bounding boxes of ink runs, used by the light-assembly entry."""
    import numpy as np
    a = np.asarray(row_img.getchannel("A"))
    cols = (a.max(axis=0) > 12).astype(np.int8)
    runs, start = [], None
    for x, on in enumerate(cols):
        if on and start is None:
            start = x
        elif not on and start is not None:
            if x - start > 2:
                runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(cols)))
    out = []
    for x0, x1 in runs:
        ang = rnd.uniform(0, 2 * math.pi)
        rad = rnd.uniform(*reg.scatter)
        out.append({"box": (x0, x1), "dx": math.cos(ang) * rad,
                    "dy": math.sin(ang) * rad * 0.8})
    return out


def render(plan: TextPlan, master: str | Path, out: str | Path, *,
           bed: str | Path | None = None, total_frames: int,
           light=None, crf: int = 16, preset: str = "slow") -> dict[str, Any]:
    """Composite the copy (and optionally stage lights) onto the master."""
    Image, _, ImageFilter, _ = _pil()
    reg = plan.register
    art = compile_plan(plan)
    w, h = plan.canvas

    dec = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(master),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                           stdout=subprocess.PIPE)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pixel_format", "rgb24",
           "-video_size", f"{w}x{h}", "-framerate", str(plan.grid.fps), "-i", "pipe:0"]
    if bed:
        cmd += ["-i", str(bed), "-map", "0:v", "-map", "1:a",
                "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    cmd += ["-c:v", "libx264", "-preset", preset, "-crf", str(crf),
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-shortest", str(out)]
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    try:
        for f in range(total_frames):
            buf = dec.stdout.read(w * h * 3)
            if len(buf) < w * h * 3:
                break
            frame = Image.frombytes("RGB", (w, h), buf).convert("RGBA")
            if light is not None:
                frame = light(frame, f)
            src_light = None
            for item in art["lines"]:
                if not (item["f_in"] <= f < item["f_out"]):
                    continue
                q = clamp((f - (item["f_out"] - reg.exit_frames)) / float(reg.exit_frames))
                out_a, out_dy = 1 - ei(q), -reg.drift * ei(q)
                pulse = 0.5 + 0.8 * math.exp(-plan.grid.frac_into_beat(f) * 4.0) \
                    if reg.pulse > 0 else 0.0
                if art["scrim"] is not None:
                    s = _alpha(art["scrim"],
                               eo(clamp((f - item["f_in"]) / 8.0)) * out_a)
                    if s:
                        frame.alpha_composite(s)
                for r in item["rows"]:
                    t0 = item["f_in"] + r["lag"]
                    p = clamp((f - t0) / float(reg.entry_frames))
                    if p <= 0:
                        continue
                    e = eo(p)
                    y = int(r["y"] + reg.rise * (1 - e) + out_dy)
                    if reg.entry == "focus":
                        layer = r["img"] if reg.blur_in * (1 - e) <= 0.35 else \
                            r["img"].filter(ImageFilter.GaussianBlur(reg.blur_in * (1 - e)))
                        layer = _alpha(layer, e * out_a)
                        if layer:
                            frame.alpha_composite(layer, (0, y))
                    elif reg.entry == "light" and p < 1.0:
                        if src_light is None:
                            src_light = _light_source(frame, w, h)
                        _assemble(frame, r, src_light, e, y, reg, w, h)
                    else:
                        if r.get("halo") is not None and reg.pulse > 0:
                            hal = _alpha(r["halo"], out_a * pulse * reg.pulse)
                            if hal:
                                frame.alpha_composite(hal, (0, y))
                        layer = _alpha(r["img"], e * out_a)
                        if layer:
                            frame.alpha_composite(layer, (0, y))
                if item["kicker"] is not None:
                    pw = max(2, int(item["kicker"].width
                                    * eo(clamp((f - item["f_in"]) / 8.0))))
                    k = _alpha(item["kicker"].crop((0, 0, pw, item["kicker"].height)),
                               out_a * (min(1.0, 0.55 + 0.6 * pulse) if reg.pulse else 1.0))
                    if k:
                        frame.alpha_composite(k, ((w - pw) // 2,
                                                  int(item["y_kicker"] + out_dy)))
            enc.stdin.write(frame.convert("RGB").tobytes())
    finally:
        try:
            enc.stdin.close()
        except Exception:
            pass
        if dec.poll() is None:
            dec.kill()
        dec.wait()
    if enc.wait():
        raise ToolError("text layer encode failed")
    return {"out": str(out), "size": art["size"], "register": reg.name,
            "lines": len(plan.lines)}


def _light_source(frame, w: int, h: int, lift: int = 420, tint: float = 0.18,
                  tint_rgb: tuple[int, int, int] = (255, 46, 136)):
    """The room's own light: the frame, blurred wide and pushed, sampled from
    ABOVE the copy — directly behind it is skin and fabric, and glyphs then
    assemble in flesh tones instead of neon."""
    Image, _, ImageFilter, _ = _pil()
    from PIL import ImageEnhance
    sm = frame.resize((w // 5, h // 5), Image.BILINEAR).filter(ImageFilter.GaussianBlur(9))
    sm = ImageEnhance.Color(sm).enhance(2.6)
    sm = ImageEnhance.Brightness(sm).enhance(1.9)
    sm = Image.blend(sm.convert("RGB"), Image.new("RGB", sm.size, tint_rgb), tint)
    big = sm.resize((w, h), Image.BILINEAR).convert("RGBA")
    out = Image.new("RGBA", (w, h))
    out.paste(big.crop((0, 0, w, h - lift)), (0, lift))
    out.paste(big.crop((0, 0, w, lift)), (0, 0))
    return out


def _assemble(frame, row, light, e: float, y: int, reg: Register, w: int, h: int) -> None:
    Image, _, ImageFilter, _ = _pil()
    mask_full = row["img"].getchannel("A")
    for i, g in enumerate(row.get("glyphs", [])):
        x0, x1 = g["box"]
        m = mask_full.crop((x0, 0, x1, mask_full.height))
        blur = reg.blur_in * (1 - e)
        ox = oy = 0
        if blur > 0.4:
            pad = int(blur * 2)
            m = m.crop((-pad, -pad, m.width + pad, m.height + pad)) \
                 .filter(ImageFilter.GaussianBlur(blur))
            ox = oy = -pad
        gx = int(x0 + ox + g["dx"] * (1 - e))
        gy = int(y + oy + g["dy"] * (1 - e))
        cx0, cy0 = max(0, gx), max(0, gy)
        cx1, cy1 = min(w, gx + m.width), min(h, gy + m.height)
        if cx1 <= cx0 or cy1 <= cy0:
            continue
        sub = m.crop((cx0 - gx, cy0 - gy, cx1 - gx, cy1 - gy))
        patch = light.crop((cx0, cy0, cx1, cy1))
        patch.putalpha(sub.point(lambda v, k=e: int(v * (0.35 + 0.65 * k))))
        frame.alpha_composite(patch, (cx0, cy0))
