"""Master render: EDL -> one ffmpeg graph -> picture, plus the audio bed.

One `filter_complex` for the whole reel, no intermediate segment files, so
there is no generation loss between the cut and the master. A source may be
opened several times as separate `-i` inputs; that is cheaper and far clearer
than juggling `split` chains.

Frame-exact trimming uses `trim=start_frame=`, never `-ss`: a fast seek lands
on a keyframe and can miss the intended frame by one or two, which silently
breaks a beat-quantised EDL.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Any

from .edl import EDL
from .util import ToolError, run

# canvas presets: name -> (w, h). "book"/"portrait" is the reel shape,
# "cinema" the horizontal one, "scope" adds hard 2.39:1 bars.
CANVAS = {
    "portrait": (1080, 1920),
    "book": (1080, 1920),
    "square": (1080, 1080),
    "feed": (1080, 1350),
    "cinema": (1920, 1080),
    "scope": (1920, 1080),
}

# xfade-based dissolves are off until they can be done with bounded memory.
# See the note in build_master: chained xfade over zoompan output was measured
# at ~100 GB RSS on a two-input graph.
_XFADE_ENABLED = False

FIT_CROP = "crop"            # fill the canvas, throw away the sides
FIT_LETTERBOX = "letterbox"  # keep the whole frame, blurred backdrop behind
FIT_PAD = "pad"              # keep the whole frame, flat black behind


def plan_fit(src_w: int, src_h: int, canvas: tuple[int, int], fit: str) -> dict[str, Any]:
    """Numbers behind the crop-vs-letterbox decision, so it can be argued.

    The counter-intuitive part, and the reason this returns `scale` for every
    option: letterboxing often DOWN-scales the picture (1280 -> 1080) while a
    hard crop UP-scales a narrow strip (405 -> 1080), so the letterbox is the
    SHARPER choice, not the softer one."""
    cw, ch = canvas
    out: dict[str, Any] = {"fit": fit, "canvas": [cw, ch], "source": [src_w, src_h]}
    if fit == FIT_CROP:
        want = cw / ch
        have = src_w / src_h
        if have > want:
            crop_w, crop_h = int(round(src_h * want)), src_h
        else:
            crop_w, crop_h = src_w, int(round(src_w / want))
        out.update({"crop": [crop_w, crop_h],
                    "kept_width_pct": round(100 * crop_w / src_w, 1),
                    "scale": round(cw / crop_w, 3)})
    else:
        pic_w = cw
        pic_h = int(round(src_h * cw / src_w / 2)) * 2
        out.update({"picture": [pic_w, pic_h],
                    "kept_width_pct": 100.0,
                    "scale": round(pic_w / src_w, 3),
                    "free_band_px": ch - pic_h})
    out["upscales"] = out["scale"] > 1.0
    return out


def _speed_chain(n_out: int, speed: float, fps: int) -> list[str]:
    """Retime to exactly `n_out` frames. Frame duplication, not interpolation:
    `minterpolate` is an order of magnitude slower and invents motion that AI
    footage — already soft in the in-betweens — does not survive well."""
    if abs(speed - 1.0) < 1e-3:
        return []
    return [f"setpts=PTS/{speed:.6f}", f"fps={fps}",
            f"trim=start_frame=0:end_frame={n_out}", "setpts=PTS-STARTPTS"]


def _zoom_chain(zoom: list[float] | None, focus: list[float] | None, n: int,
                canvas: tuple[int, int], fps: int, *, punch: float = 0.0,
                punch_frames: int = 7) -> str:
    """A linear push-in that also does the upscale.

    `zoompan` rounds its crop origin to whole pixels, which shows as jitter when
    it runs at source resolution. Letting it output the CANVAS size instead
    makes that rounding sub-pixel relative to the delivered frame, so the move
    stays smooth — and it replaces the separate scale step rather than adding to
    it."""
    cw, ch = canvas
    z0, z1 = (float(zoom[0]), float(zoom[1])) if zoom else (1.0, 1.0)
    fx, fy = (focus or [0.5, 0.5])[0], (focus or [0.5, 0.5])[1]
    span = max(1, n - 1)
    z = f"{z0:.5f}+({z1 - z0:.5f})*on/{span}"
    if punch and punch > 1.0:
        pf = max(1, int(punch_frames))
        # lands at `punch` on the cut, settles to the drift curve within pf
        z = f"({z})+{punch - 1.0:.5f}*max(0\\,1-on/{pf})"
    return (f"zoompan=z='{z}':d=1:"
            f"x='(iw-iw/zoom)*{fx:.4f}':y='(ih-ih/zoom)*{fy:.4f}':"
            f"s={cw}x{ch}:fps={fps}")


def _shot_chain(i: int, a: int, b: int, *, canvas: tuple[int, int], fit: str,
                src_w: int, src_h: int, crop_x: int | None, hflip: bool,
                sharpen: bool, grade: str = "", zoom: list[float] | None = None,
                focus: list[float] | None = None, speed: float = 1.0,
                n_out: int | None = None, fps: int = 24,
                punch: float = 0.0, punch_frames: int = 7, ca: int = 0) -> str:
    cw, ch = canvas
    chain = [f"[{i}:v]trim=start_frame={a}:end_frame={b}", "setpts=PTS-STARTPTS"]
    chain += _speed_chain(n_out or (b - a), speed, fps)
    if grade:
        chain.append(grade)            # per-clip colour match, before any look
    if ca > 0:
        # 2-frame chromatic split on the cut: reads as impact, gone before the
        # eye can call it an effect
        chain.append(f"rgbashift=rh=3:bh=-3:enable='lt(t,{ca / fps:.4f})'")
    if fit == FIT_CROP:
        want = cw / ch
        have = src_w / src_h
        if have > want:
            crop_w, crop_h = int(round(src_h * want)), src_h
            x = (src_w - crop_w) // 2 if crop_x is None else max(0, min(crop_x, src_w - crop_w))
            chain.append(f"crop={crop_w}:{crop_h}:{x}:0")
        else:
            crop_w, crop_h = src_w, int(round(src_w / want))
            y = (src_h - crop_h) // 2
            chain.append(f"crop={crop_w}:{crop_h}:0:{y}")
        if hflip:
            chain.append("hflip")
        if zoom or punch:
            # zoompan delivers the canvas itself — no separate scale step
            chain.append(_zoom_chain(zoom, focus, n_out or (b - a), (cw, ch), fps,
                                     punch=punch, punch_frames=punch_frames))
        else:
            chain.append(f"scale={cw}:{ch}:flags=lanczos")
        if sharpen:
            chain.append("unsharp=5:5:0.42:5:5:0.0")
    else:
        if hflip:
            chain.append("hflip")
        if zoom or punch:
            chain.append(_zoom_chain(zoom, focus, n_out or (b - a), (src_w, src_h),
                                     fps, punch=punch, punch_frames=punch_frames))
    # Normalise the timebase in constant time. Two wrong ways were measured here:
    #   `fps=N`   re-derives frames from timestamps and buffers without bound —
    #             a two-input graph reached ~100 GB RSS and took the machine down.
    #   `settb`   alone RE-LABELS the timebase without rescaling PTS, so a branch
    #             still on the source tb (1/12288) suddenly claims 512 s where it
    #             meant 1 s; the encoder then tries to fill the gap and hangs.
    # `setpts=N` rewrites PTS from the frame index, which is exactly right once
    # the timebase is 1/fps.
    chain += [f"settb=1/{fps}", "setpts=N", "setsar=1"]
    return ",".join(chain) + f"[v{i}]"


def build_master(edl: EDL, out: str | Path, *, canvas: str = "portrait",
                 fit: str = FIT_CROP, grade: str | None = None,
                 vignette: bool = False, fade_out_frames: int = 12,
                 crf: int = 16, preset: str = "slow",
                 backdrop_blur: int = 42, filter_dump: str | Path | None = None,
                 src_size: tuple[int, int] | None = None) -> dict[str, Any]:
    """Render the picture master (no audio). Returns a small report."""
    cw, ch = CANVAS[canvas]
    if src_size is None:
        from .util import probe_json
        first = edl.sources[edl.shots[0].source]
        st = next(s for s in probe_json(first)["streams"] if s.get("codec_type") == "video")
        src_size = (int(st["width"]), int(st["height"]))
    src_w, src_h = src_size

    durs = edl.durations
    fps = edl.grid.fps
    # A shot that dissolves into the next must supply the overlap frames as
    # well, otherwise xfade eats them out of its own running time and the whole
    # timeline shortens — the cuts would drift off the beat one dissolve at a
    # time.
    extra = [(s.dissolve if i < len(edl.shots) - 1 else 0)
             for i, s in enumerate(edl.shots)]
    parts, labels, inputs = [], [], []
    for i, (shot, n) in enumerate(zip(edl.shots, durs, strict=False)):
        a = edl.in_frame(shot)
        n_full = n + extra[i]
        read = max(1, round(n_full * shot.source_frames_per_output))
        inputs.append(edl.sources[shot.source])
        parts.append(_shot_chain(i, a, a + read, canvas=(cw, ch), fit=fit,
                                 src_w=src_w, src_h=src_h, crop_x=shot.crop_x,
                                 hflip=shot.hflip,
                                 sharpen=(cw / max(1, src_w)) > 1.2 and not shot.zoom,
                                 grade=shot.grade, zoom=shot.zoom,
                                 focus=shot.focus, speed=shot.speed,
                                 n_out=n_full, fps=fps, punch=shot.punch,
                                 punch_frames=shot.punch_frames, ca=shot.ca))
        labels.append(f"[v{i}]")

    # xfade holds every frame of its first input in memory until the transition
    # point. Chained across a whole film, on 1080x1920 frames produced by
    # zoompan, that is a memory bomb — killed at ~100 GB on this machine. Until
    # dissolves are done a bounded way (per-pair intermediates), they are
    # rendered as hard cuts and the caller is told, rather than silently
    # dropping something that was asked for.
    if any(extra) and not _XFADE_ENABLED:
        extra = [0] * len(extra)
    if any(extra):
        acc, acc_len = "[v0]", durs[0] + extra[0]
        for i in range(1, len(edl.shots)):
            d = extra[i - 1]
            nxt = f"[x{i}]" if i < len(edl.shots) - 1 else "[cat]"
            if d > 0:
                off = (acc_len - d) / fps
                parts.append(f"{acc}[v{i}]xfade=transition=fade:"
                             f"duration={d / fps:.4f}:offset={off:.4f}{nxt}")
                acc_len = acc_len + durs[i] + extra[i] - d
            else:
                parts.append(f"{acc}[v{i}]concat=n=2:v=1:a=0{nxt}")
                acc_len = acc_len + durs[i] + extra[i]
            acc = nxt
    else:
        parts.append("".join(labels) + f"concat=n={len(edl.shots)}:v=1:a=0[cat]")

    tail = "[cat]"
    if fit in (FIT_LETTERBOX, FIT_PAD):
        pic_h = int(round(src_h * cw / src_w / 2)) * 2
        pic_y = max(0, (ch - pic_h) // 2)
        if canvas in ("portrait", "book"):
            # sit the picture slightly high so the free band below can hold the
            # copy without fighting the platform's caption zone
            pic_y = max(0, int((ch - pic_h) * 0.42) // 2 * 2)
        parts.append("[cat]split=2[bg][fg]")
        if fit == FIT_LETTERBOX:
            parts.append(f"[bg]scale={cw}:{ch}:force_original_aspect_ratio=increase,"
                         f"crop={cw}:{ch},gblur=sigma={backdrop_blur},"
                         f"eq=brightness=-0.16:saturation=0.55:contrast=0.92[bgb]")
        else:
            parts.append(f"[bg]scale={cw}:{ch},drawbox=x=0:y=0:w={cw}:h={ch}:"
                         f"color=black:t=fill[bgb]")
        fg = f"[fg]scale={cw}:{pic_h}:flags=lanczos"
        if grade:
            fg += f",{grade}"
        parts.append(fg + "[fgs]")
        parts.append(f"[bgb][fgs]overlay=0:{pic_y}[ov]")
        tail = "[ov]"
    elif grade:
        parts.append(f"[cat]{grade}[gr]")
        tail = "[gr]"

    post = []
    if canvas == "scope":
        bar = int(round((ch - cw / 2.39) / 2))
        post.append(f"drawbox=x=0:y=0:w={cw}:h={bar}:color=black:t=fill")
        post.append(f"drawbox=x=0:y={ch - bar}:w={cw}:h={bar}:color=black:t=fill")
    if vignette:
        post.append("vignette=PI/5")
    if fade_out_frames > 0:
        st = (edl.total_frames - fade_out_frames) / edl.grid.fps
        post.append(f"fade=t=out:st={st:.4f}:d={fade_out_frames / edl.grid.fps:.4f}")
    post.append("format=yuv420p")
    parts.append(tail + ",".join(post) + "[vout]")

    graph = ";\n".join(parts)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    script = Path(filter_dump) if filter_dump else out.with_suffix(".filter.txt")
    script.write_text(graph, encoding="utf-8")

    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    for path in inputs:
        cmd += ["-i", str(path)]
    cmd += ["-filter_complex_script", str(script), "-map", "[vout]",
            "-r", str(edl.grid.fps), "-c:v", "libx264", "-preset", preset,
            "-crf", str(crf), "-pix_fmt", "yuv420p", str(out)]
    run(cmd, capture=True)
    return {"master": str(out), "filter": str(script), "canvas": [cw, ch],
            "fit": fit, "frames": edl.total_frames,
            "duration": round(edl.duration, 4),
            "plan": plan_fit(src_w, src_h, (cw, ch), fit)}


def _loudnorm_spec(source: str | Path | None, lufs: float, *,
                   start: float = 0.0, duration: float | None = None) -> str:
    """Two-pass `loudnorm`, falling back to one pass if the analysis fails.

    Single-pass loudnorm is a look-ahead estimate and drifts on long, dynamic
    material: measured on a 4-minute track with LRA 6.8 it landed 1.4 LU hot
    with the true peak up at -0.8 dBFS, i.e. no headroom left for the platform's
    re-encode. The measured pass removes both errors."""
    import json as _json
    base = f"loudnorm=I={lufs}:TP=-1.5:LRA=11"
    if source is None:
        return base
    cmd = ["ffmpeg", "-hide_banner", "-nostats"]
    if start:
        cmd += ["-ss", f"{start:.6f}"]
    cmd += ["-i", str(source)]
    if duration:
        cmd += ["-t", f"{duration:.6f}"]
    cmd += ["-af", base + ":print_format=json", "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True)
    text = (p.stderr or b"").decode("utf-8", "replace")
    try:
        blob = text[text.rindex("{"):text.rindex("}") + 1]
        m = _json.loads(blob)
        return (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
                f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}"
                f":offset={m['target_offset']}:linear=true")
    except (ValueError, KeyError):
        return base


def build_bed(out: str | Path, *, music: str | Path | None = None,
              start: float = 0.0, duration: float, loop_source: str | Path | None = None,
              loop_in: float = 0.0, loop_len: float | None = None,
              lufs: float = -14.0, fade_in: float = 0.0, fade_out: float = 0.6) -> dict[str, Any]:
    """One continuous audio bed, normalised, at 48 kHz.

    `aresample=48000` after `loudnorm` is mandatory, not cosmetic: the filter
    outputs 192 kHz, and a downstream AAC encode without an explicit `-ar`
    silently settles on 96 kHz (vault: AUTO-REELS learning, 2026-07-31)."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ln = _loudnorm_spec(music or loop_source, lufs, start=start, duration=duration)
    af = [ln, "aresample=48000"]
    if fade_in > 0:
        af.append(f"afade=t=in:st=0:d={fade_in:.4f}")
    if fade_out > 0:
        af.append(f"afade=t=out:st={max(0.0, duration - fade_out):.4f}:d={fade_out:.4f}")

    if music:
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{start:.6f}", "-i", str(music), "-t", f"{duration:.6f}"]
    elif loop_source:
        if not loop_len:
            raise ToolError("loop_len required when looping a source clip")
        seg = out.parent / "_bed_seg.wav"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(loop_source),
             "-af", f"atrim={loop_in:.6f}:{loop_in + loop_len:.6f},asetpts=PTS-STARTPTS",
             "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", str(seg)])
        loops = max(1, int(duration / loop_len + 0.999))
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-stream_loop", str(loops - 1), "-i", str(seg), "-t", f"{duration:.6f}"]
    else:
        raise ToolError("either music= or loop_source= is required")

    cmd += ["-af", ",".join(af), "-ac", "2", "-c:a", "pcm_s16le", str(out)]
    run(cmd)
    return {"bed": str(out), "duration": round(duration, 4),
            "seams": [] if music else [i * (loop_len or 0)
                                       for i in range(1, int(duration / (loop_len or 1)) + 1)]}


def shutter_wav(out: str | Path, *, sr: int = 48000) -> str:
    """A synthesised SLR shutter.

    TWO transients is the whole trick: the mirror slaps up, and ~45 ms later the
    curtain closes. A single blip — the obvious first attempt — reads as a UI
    click, not as a camera, no matter how it is filtered. The faint decay
    between them is the motor, and it is deliberately weak: made audible it
    turns into a toy winder.

    The envelope MUST be applied with `aeval`, not with `volume=...:eval=frame`.
    `volume` re-evaluates once per audio FRAME — 1024 samples, i.e. ~21 ms at
    48 kHz — so a 7 ms transient sitting at 45 ms falls between two evaluations
    and never happens. Measured: the second slap came out ~25 dB down and the
    click collapsed back into a single blip. `aeval` evaluates per sample."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    env = ("exp(-t/0.0045)"
           "+0.72*gte(t,0.045)*exp(-(t-0.045)/0.0075)"
           "+0.16*gte(t,0.012)*lt(t,0.045)*exp(-(t-0.012)/0.026)")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anoisesrc=c=white:r={sr}:d=0.14:a=0.9",
         "-af", (f"aeval=exprs='val(0)*({env})':channel_layout=mono,"
                 "highpass=f=780,lowpass=f=9500,"
                 f"aresample={sr}"),
         "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return str(out)


def mix_sfx(bed: str | Path, out: str | Path,
            hits: list[dict[str, Any]] | list[float], *,
            sound: str | Path, gain_db: float = -9.0,
            sr: int = 48000) -> dict[str, Any]:
    """Mix a one-shot sound into a bed at given times.

    Each hit may carry an `amp`; the click is scaled by it so a bar flash sounds
    like a bar flash. The limiter at the end is not optional — a dozen
    transients summed onto a bed that was already normalised to -14 LUFS will
    put the true peak over the platform's ceiling, and that is a defect the
    loudness number alone does not show."""
    norm: list[tuple[float, float]] = []
    for h in hits:
        if isinstance(h, dict):
            norm.append((float(h["t"]), float(h.get("amp", 1.0) or 1.0)))
        else:
            norm.append((float(h), 1.0))
    if not norm:
        raise ToolError("no hit times given — nothing to mix")
    peak = max(a for _, a in norm) or 1.0

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(bed)]
    parts, labels = [], ["[0:a]"]
    for i, (t, amp) in enumerate(norm, start=1):
        cmd += ["-i", str(sound)]
        ms = max(0, int(round(t * 1000)))
        rel = 20.0 * math.log10(max(0.3, amp / peak))
        parts.append(f"[{i}:a]adelay={ms}:all=1,volume={gain_db + rel:.2f}dB,"
                     f"aformat=channel_layouts=stereo[c{i}]")
        labels.append(f"[c{i}]")
    parts.append("".join(labels)
                 + f"amix=inputs={len(labels)}:normalize=0:duration=first,"
                   f"alimiter=limit=0.89,aresample={sr}[a]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[a]",
            "-ac", "2", "-c:a", "pcm_s16le", str(out)]
    run(cmd)
    return {"out": str(out), "mixed": len(norm),
            "times": [round(t, 4) for t, _ in norm]}


def mux(video: str | Path, audio: str | Path, out: str | Path, *,
        abitrate: str = "192k") -> str:
    """Copy the picture, encode the bed. `-ar 48000` is explicit on purpose."""
    out = Path(out)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(video), "-i", str(audio), "-map", "0:v", "-map", "1:a",
         "-c:v", "copy", "-c:a", "aac", "-b:a", abitrate, "-ar", "48000",
         "-movflags", "+faststart", "-shortest", str(out)])
    return str(out)
