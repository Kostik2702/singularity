"""`reel` — the deterministic half of reel editing.

Every subcommand prints a typed envelope with `--json` and exits 0 on success,
2 when the TOOL could not do its job. Exit 2 never means "the footage is bad".
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import ENVELOPE
from .util import ToolError, emit, envelope, require_ffmpeg


def _p(sub, name, help_):
    p = sub.add_parser(name, help=help_)
    p.add_argument("--json", action="store_true", help="print the typed envelope")
    return p


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="reel", description=f"reel editing engine ({ENVELOPE})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = _p(sub, "probe", "intake: metadata, byte-identical duplicates, loudness spread")
    p.add_argument("target")
    p.add_argument("--no-loudness", action="store_true")

    p = _p(sub, "sheets", "contact sheets: overview + dense with timecodes")
    p.add_argument("target")
    p.add_argument("--out", required=True)
    p.add_argument("--step", type=float, default=0.5, help="seconds between dense frames")

    p = _p(sub, "beats", "tempo, downbeat phase and confidence")
    p.add_argument("audio")

    p = _p(sub, "sections", "per-bar energy map and a suggested window")
    p.add_argument("audio")
    p.add_argument("--bpm", type=float)
    p.add_argument("--phase", type=float)
    p.add_argument("--bars", type=int, default=16, help="length of the window to suggest")

    p = _p(sub, "fonts", "list, fetch or measure typefaces")
    p.add_argument("action", choices=["list", "fetch", "measure"])
    p.add_argument("--register", default="display-condensed",
                   help="display-condensed | serif-editorial | sans-neutral")
    p.add_argument("--dest", help="download directory (fetch/measure)")
    p.add_argument("--line", action="append", default=[],
                   help="a real line of the copy; repeat for each")
    p.add_argument("--max-width", type=int, default=912)
    p.add_argument("--weight", type=int)
    p.add_argument("--track", type=float, default=0.0)

    p = _p(sub, "match", "measure colour drift across clips and emit per-clip corrections")
    p.add_argument("target", help="folder (recursed) or a single file")
    p.add_argument("--out", required=True, help="where to write the match map")
    p.add_argument("--reference", help="filename whose look everything matches; "
                                       "default is the median of all clips")
    p.add_argument("--strength", type=float, default=0.8,
                   help="0..1; below 1 keeps deliberate differences (a fire scene "
                        "should stay warmer than a crypt)")
    p.add_argument("--mode", default="luma", choices=["luma", "rgb"],
                   help="luma (default) equalises exposure/contrast only and lets "
                        "one shared look unify the palette; rgb also matches "
                        "colour and will flatten deliberately-tinted scenes")
    p.add_argument("--samples", type=int, default=9)

    p = _p(sub, "cast", "scaffold or inspect the casting sheet (verdict + story stage)")
    p.add_argument("action", choices=["new", "show"])
    p.add_argument("target", help="footage folder (new) or a casting sheet (show)")
    p.add_argument("--out", help="where to write the sheet (new)")

    p = _p(sub, "validate", "check an EDL without rendering anything")
    p.add_argument("edl")
    p.add_argument("--cast", help="casting sheet; applies story stages and refuses "
                                  "shots taken from rejected or unjudged clips")

    p = _p(sub, "build", "render the picture master from an EDL")
    p.add_argument("edl")
    p.add_argument("--out", required=True)
    p.add_argument("--canvas", default="portrait",
                   help="portrait/book | cinema | scope | square | feed")
    p.add_argument("--fit", default="crop", choices=["crop", "letterbox", "pad"])
    p.add_argument("--grade", default="none")
    p.add_argument("--look", default="none",
                   help="creative look applied AFTER matching: "
                        "gothic-ember | gothic-moon | bleach-bone")
    p.add_argument("--match", help="match map from `reel match`; its per-clip "
                                   "correction is applied before the look")
    p.add_argument("--grain", type=float, default=0.0)
    p.add_argument("--vignette", action="store_true")
    p.add_argument("--fade-out", type=int, default=12, help="frames")

    p = _p(sub, "bed", "build the continuous audio bed")
    p.add_argument("--out", required=True)
    p.add_argument("--duration", type=float, required=True)
    p.add_argument("--music")
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--loop-source")
    p.add_argument("--loop-in", type=float, default=0.0)
    p.add_argument("--loop-len", type=float)
    p.add_argument("--lufs", type=float, default=-14.0)
    p.add_argument("--fade-in", type=float, default=0.0)
    p.add_argument("--fade-out", type=float, default=0.6)

    p = _p(sub, "mux", "copy the picture, encode the bed, faststart")
    p.add_argument("video")
    p.add_argument("audio")
    p.add_argument("--out", required=True)

    p = _p(sub, "text", "composite an animated copy plan onto a master")
    p.add_argument("master")
    p.add_argument("plan")
    p.add_argument("--out", required=True)
    p.add_argument("--bed")

    p = _p(sub, "light", "flash pass over a finished master (WCAG-gated)")
    p.add_argument("master")
    p.add_argument("--out", required=True)
    p.add_argument("--bpm", type=float, required=True)
    p.add_argument("--phase", type=float, default=0.0)
    p.add_argument("--fps", type=int, default=24)
    p.add_argument("--beats-per-bar", type=int, default=4)
    p.add_argument("--rig", default="camera", choices=["camera", "stage"],
                   help="camera = white xenon, flat, fast; stage = the coloured desk arc")
    p.add_argument("--bar-amp", type=float, default=0.85)
    p.add_argument("--beat-amp", type=float, default=0.30)
    p.add_argument("--decay", type=float, default=13.0)
    p.add_argument("--bed", help="mux this audio while re-encoding")
    p.add_argument("--report", help="write the fired-hit list here; feeds `reel sfx`")

    p = _p(sub, "sfx", "mix a one-shot sound into a bed at given times")
    p.add_argument("bed")
    p.add_argument("--out", required=True)
    p.add_argument("--sound", default="shutter",
                   help="'shutter' to synthesise an SLR shutter, or a path to a wav")
    p.add_argument("--at", help="comma-separated seconds")
    p.add_argument("--from-light", help="hit report written by `reel light --report`")
    p.add_argument("--gain", type=float, default=-9.0, help="dB relative to the bed")

    p = _p(sub, "cuts", "before/after sheet at every cut of an EDL")
    p.add_argument("master")
    p.add_argument("edl")
    p.add_argument("--out", required=True)
    p.add_argument("--crop", help="ffmpeg crop expression, e.g. crop=1080:420:0:1120")

    p = _p(sub, "qa", "loudness, sample rate and WCAG flash check on a master")
    p.add_argument("master")
    p.add_argument("--no-flash", action="store_true")
    p.add_argument("--target-lufs", type=float, default=-14.0)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_ffmpeg()
        return _dispatch(args)
    except ToolError as exc:
        return emit(envelope(False, {}, [str(exc)]), getattr(args, "json", False),
                    f"reel: {exc}")
    except Exception as exc:                                  # pragma: no cover
        return emit(envelope(False, {}, [f"{type(exc).__name__}: {exc}"]),
                    getattr(args, "json", False), f"reel: {exc}")


def _dispatch(args) -> int:                                    # noqa: C901
    if args.cmd == "probe":
        from .probe import inspect
        data = inspect(args.target, with_loudness=not args.no_loudness)
        human = [f"{len(data['videos'])} video, {len(data['audio'])} audio; "
                 f"{len(data['unique_videos'])} unique"]
        for r in data["videos"]:
            human.append("  %-38s %6.2fs %sx%s %.4g fps %s%s"
                         % (r["name"][:38], r["duration"], r.get("width"), r.get("height"),
                            r.get("fps", 0), r.get("vcodec", ""),
                            "  DUPLICATE of " + r["duplicate_of"] if r["duplicate_of"] else ""))
        human += ["  ! " + w for w in data["warnings"]]
        return emit(envelope(not data["problems"], data, data["problems"]),
                    args.json, "\n".join(human))

    if args.cmd == "sheets":
        from .probe import inspect
        from .sheets import build_all
        inv = inspect(args.target, with_loudness=False)
        targets = [v for v in inv["videos"] if v["duplicate_of"] is None]
        data = build_all(targets, args.out, step_sec=args.step)
        return emit(envelope(not data["problems"], data, data["problems"]), args.json,
                    "\n".join(f"{s['name']}\n  {s['overview']}\n  {s['dense']}"
                              for s in data["sheets"]))

    if args.cmd == "beats":
        from .music import detect_beats
        t = detect_beats(args.audio)
        data = {"bpm": t.bpm, "phase": t.phase, "beat": round(t.beat, 6),
                "bar": round(t.beat * 4, 6), "confidence": t.confidence,
                "candidates": t.candidates}
        return emit(envelope(True, data), args.json,
                    f"{t.bpm} BPM  beat {t.beat:.4f}s  bar {t.beat * 4:.4f}s  "
                    f"downbeat at {t.phase:.3f}s  (2nd candidate {t.confidence:.3f})")

    if args.cmd == "sections":
        from .music import detect_beats, energy_map, suggest_section
        bpm, phase = args.bpm, args.phase
        if bpm is None or phase is None:
            t = detect_beats(args.audio)
            bpm, phase = t.bpm, t.phase
        bars = energy_map(args.audio, bpm, phase)
        pick = suggest_section(bars, args.bars)
        data = {"bpm": bpm, "phase": phase, "bars": bars, "suggested": pick}
        peak = max((b["rms"] for b in bars), default=1.0) or 1.0
        human = ["bar    t(s)    RMS   bright  level"]
        for b in bars:
            human.append("%4d %7.2f  %.3f  %.3f  %s"
                         % (b["bar"], b["t"], b["rms"], b["bright"],
                            "#" * int(round(b["rms"] / peak * 40))))
        human.append(f"\nsuggested: bar {pick['start_bar']} @ {pick['t']:.2f}s "
                     f"for {args.bars} bars — {pick['why']}")
        return emit(envelope(True, data), args.json, "\n".join(human))

    if args.cmd == "fonts":
        from . import fonts as F
        if args.action == "list":
            data = {"catalog": F.CATALOG, "system_fallback": F.SYSTEM_FALLBACK}
            return emit(envelope(True, data), args.json,
                        "\n".join(f"{reg}:\n" + "\n".join("  " + e["name"] for e in ents)
                                  for reg, ents in F.CATALOG.items()))
        if not args.dest:
            raise ToolError("--dest is required for fetch/measure")
        got = F.fetch_register(args.register, args.dest)
        if args.action == "fetch":
            return emit(envelope(all("error" not in g for g in got), {"fonts": got},
                                 [g["error"] for g in got if "error" in g]),
                        args.json, "\n".join(g.get("roman", g.get("error", "")) for g in got))
        if not args.line:
            raise ToolError("--line is required for measure (give the real copy)")
        rows = F.measure(got, args.line, args.max_width,
                         weight=args.weight, track=args.track)
        human = ["face                      size  widest"]
        for r in rows:
            human.append("%-24s  %4s  %6s" % (r["name"], r.get("size", "-"),
                                              r.get("widest", r.get("error", ""))))
        human.append("\nlargest size at the same line width wins — taste is not the metric")
        return emit(envelope(True, {"ranking": rows, "max_width": args.max_width}),
                    args.json, "\n".join(human))

    if args.cmd == "match":
        from .colour import build_map, save_map
        from .probe import VIDEO_EXT
        t = Path(args.target)
        files = sorted(p for p in t.rglob("*") if p.suffix.lower() in VIDEO_EXT) \
            if t.is_dir() else [t]
        if not files:
            raise ToolError(f"no video files under {t}")
        m = build_map([str(f) for f in files], reference=args.reference,
                      strength=args.strength, mode=args.mode, samples=args.samples)
        if "clips" not in m:
            return emit(envelope(False, m, m.get("problems", [])), args.json,
                        "\n".join(m.get("problems", [])))
        save_map(m, args.out)
        rows = sorted(m["clips"].items(), key=lambda kv: -kv[1]["drift_from_target"])
        human = [f"target {m['target']['mean']} ({m['target']['from']}), "
                 f"strength {m['strength']}",
                 "самые уехавшие по цвету:"]
        for path, r in rows[:8]:
            human.append("  %-46s drift %.3f  luma %.3f  sat %.3f"
                         % (Path(path).parent.name + "/" + Path(path).name[-20:],
                            r["drift_from_target"], r["luma"], r["saturation"]))
        human.append(f"\n-> {args.out}  ({len(rows)} клипов)")
        return emit(envelope(not m["problems"], m, m["problems"]), args.json,
                    "\n".join(human))

    if args.cmd == "cast":
        from .casting import load, save, scaffold, story_table, undecided
        if args.action == "new":
            from .probe import inspect
            if not args.out:
                raise ToolError("--out is required for `cast new`")
            t = Path(args.target)
            clips = []
            for d in ([t] if not t.is_dir() else
                      sorted([p for p in t.iterdir() if p.is_dir()]) or [t]):
                clips += inspect(d, with_loudness=False)["videos"]
            if not clips:
                clips = inspect(t, with_loudness=False)["videos"]
            sheet = scaffold([c for c in clips if not c.get("duplicate_of")])
            save(sheet, args.out)
            return emit(envelope(True, {"clips": len(sheet["clips"]), "out": args.out}),
                        args.json,
                        f"{args.out}: {len(sheet['clips'])} клипов, все с verdict '?'.\n"
                        "Заполнять, ЧИТАЯ контактные листы: verdict, depicts, stage, "
                        "usable. Пока есть '?', сборка не пройдёт валидацию.")
        sheet = load(args.target)
        left = undecided(sheet)
        return emit(envelope(not left, sheet, [f"{len(left)} клип(ов) без вердикта"]
                             if left else []),
                    args.json, story_table(sheet)
                    + (f"\n\nбез вердикта: {', '.join(left)}" if left else
                       "\n\nвсе клипы отсуждены."))

    if args.cmd in ("validate", "build", "cuts"):
        from .edl import EDL
        from .util import probe_json
        edl = EDL.load(args.edl)
        source_frames: dict[str, int] = {}
        for key, path in edl.sources.items():
            j = probe_json(path)
            st = next(s for s in j["streams"] if s.get("codec_type") == "video")
            num, den = (st.get("r_frame_rate", "24/1").split("/") + ["1"])[:2]
            fps = float(num) / float(den or 1)
            source_frames[key] = int(float(j["format"]["duration"]) * fps)

        if args.cmd == "validate":
            problems = []
            if getattr(args, "cast", None):
                from .casting import apply_to, load as load_cast
                problems += apply_to(load_cast(args.cast), edl)
            problems += edl.validate(source_frames)
            return emit(envelope(not problems, {"table": edl.table(),
                                                "frames": edl.total_frames,
                                                "duration": round(edl.duration, 4)}, problems),
                        args.json, edl.table() + ("\n\nPROBLEMS:\n  " + "\n  ".join(problems)
                                                  if problems else "\n\nEDL is renderable."))
        if args.cmd == "cuts":
            from .sheets import cuts as cut_sheet
            out = cut_sheet(args.master, args.out, edl.edges[1:-1], crop=args.crop)
            return emit(envelope(True, {"sheet": str(out), "cuts": len(edl.edges) - 2}),
                        args.json, f"{out}\nread it for jump cuts, axis flips and any "
                                   f"backwards step of the emotional ladder")

        from .colour import LOOKS, load_map, texture
        from .effects import GRADES
        from .render import build_master
        problems = edl.validate(source_frames)
        if problems:
            return emit(envelope(False, {"table": edl.table()}, problems), args.json,
                        "EDL problems:\n  " + "\n  ".join(problems))
        if args.match:
            m = load_map(args.match)
            missing = []
            for shot in edl.shots:
                path = edl.sources[shot.source]
                rec = m["clips"].get(path) or m["clips"].get(str(Path(path)))
                if rec is None:
                    missing.append(path)
                elif not shot.grade:
                    shot.grade = rec["filter"]
            if missing:
                problems.append(f"{len(missing)} source(s) absent from the match map")
        look = LOOKS.get(args.look, "")
        tex = texture(grain=args.grain, vignette=5.0 if args.vignette else 0.0)
        creative = ",".join(x for x in (GRADES.get(args.grade) or "", look, tex) if x)
        rep = build_master(edl, args.out, canvas=args.canvas, fit=args.fit,
                           grade=creative or None,
                           vignette=False, fade_out_frames=args.fade_out)
        rep["look"] = args.look
        rep["matched"] = bool(args.match)
        plan = rep["plan"]
        return emit(envelope(True, rep), args.json,
                    f"{rep['master']}\n{rep['frames']} frames = {rep['duration']}s, "
                    f"{plan['canvas'][0]}x{plan['canvas'][1]}, fit={plan['fit']}, "
                    f"scale x{plan['scale']} ({'up' if plan['upscales'] else 'down'}), "
                    f"width kept {plan['kept_width_pct']}%")

    if args.cmd == "bed":
        from .render import build_bed
        rep = build_bed(args.out, music=args.music, start=args.start,
                        duration=args.duration, loop_source=args.loop_source,
                        loop_in=args.loop_in, loop_len=args.loop_len, lufs=args.lufs,
                        fade_in=args.fade_in, fade_out=args.fade_out)
        return emit(envelope(True, rep), args.json, f"{rep['bed']} ({rep['duration']}s)")

    if args.cmd == "mux":
        from .render import mux
        out = mux(args.video, args.audio, args.out)
        return emit(envelope(True, {"out": out}), args.json, out)

    if args.cmd == "light":
        from .edl import Grid
        from .effects import LightRig, act_arc, apply_rig, camera_rig
        from .util import probe_json as _pj

        mj = _pj(args.master)
        mv = next(s for s in mj["streams"] if s.get("codec_type") == "video")
        w, h = int(mv["width"]), int(mv["height"])
        total_frames = int(mv.get("nb_frames") or 0)
        if not total_frames:
            raise ToolError(f"cannot read a frame count from {args.master}")
        grid = Grid(bpm=args.bpm, fps=args.fps, phase=args.phase,
                    beats_per_bar=args.beats_per_bar)
        total_beats = total_frames / grid.beat_frames
        if args.rig == "camera":
            rig = camera_rig(grid, total_beats, bar_amp=args.bar_amp,
                             beat_amp=args.beat_amp, decay=args.decay)
        else:
            rig = LightRig(grid=grid, acts=act_arc(total_beats,
                                                   beats_per_bar=args.beats_per_bar))
        rep = apply_rig(args.master, args.out, rig, total_frames=total_frames,
                        canvas=(w, h), bed=args.bed)
        if args.report:
            Path(args.report).write_text(
                json.dumps({"grid": {"bpm": args.bpm, "fps": args.fps,
                                     "phase": args.phase,
                                     "beats_per_bar": args.beats_per_bar},
                            "hits": rep["hits"]}, ensure_ascii=False, indent=2),
                encoding="utf-8")
        bars = sum(1 for x in rep["hits"] if x["bar"])
        return emit(envelope(True, rep), args.json,
                    f"{rep['out']}\n{rep['fired']} flashes ({bars} on the bar, "
                    f"{rep['fired'] - bars} on the beat), {rep['per_second']}/s "
                    f"— WCAG 2.3.1 ceiling is 3/s")

    if args.cmd == "sfx":
        from .render import mix_sfx, shutter_wav

        hits: list = []
        if args.from_light:
            hits = json.loads(Path(args.from_light).read_text(encoding="utf-8"))["hits"]
        elif args.at:
            hits = [float(x) for x in args.at.split(",") if x.strip()]
        else:
            raise ToolError("give either --at or --from-light")
        sound = args.sound
        if sound == "shutter":
            sound = shutter_wav(Path(args.out).parent / "_shutter.wav")
        rep = mix_sfx(args.bed, args.out, hits, sound=sound, gain_db=args.gain)
        return emit(envelope(True, rep), args.json,
                    f"{rep['out']}  {rep['mixed']} hits mixed at {args.gain:g} dB")

    if args.cmd == "text":
        from .edl import Grid
        from .textlayer import (Line, TextPlan, club_register, editorial_register,
                                render)
        spec = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        g = spec["grid"]
        grid = Grid(bpm=float(g["bpm"]), fps=int(g.get("fps", 24)),
                    phase=float(g.get("phase", 0.0)))
        rspec = spec["register"]
        if rspec.get("preset") == "club":
            reg = club_register(rspec["font_roman"])
        elif rspec.get("preset") == "editorial":
            reg = editorial_register(rspec["font_roman"], rspec["font_italic"])
        else:
            from .textlayer import Register
            reg = Register(**{k: v for k, v in rspec.items() if k != "preset"})
        for k, v in (rspec.get("overrides") or {}).items():
            setattr(reg, k, v)
        # A stale master is the trap this guard exists for: when `build` fails,
        # the previous master is still on disk, and laying copy over it produces
        # a file that opens, plays and is WRONG. Refuse unless the master is
        # exactly as long as the plan expects.
        from .util import probe_json as _pj
        want = int(spec["total_frames"])
        mj = _pj(args.master)
        mv = next(s for s in mj["streams"] if s.get("codec_type") == "video")
        have = int(mv.get("nb_frames") or 0)
        # concat legitimately loses a frame or two at branch boundaries, so the
        # tolerance is proportional. A stale master is off by hundreds, not by
        # two — the guard must catch that without crying over rounding.
        tol = max(3, want // 200)
        if have and abs(have - want) > tol:
            raise ToolError(
                f"master has {have} frames but the text plan expects {want} "
                f"(допуск {tol}) — почти наверняка мастер устарел: сборка упала, "
                "а файл остался от прошлого прогона. Пересобрать build и повторить.")

        plan = TextPlan(grid=grid, register=reg,
                        lines=[Line(**ln) for ln in spec["lines"]],
                        canvas=tuple(spec.get("canvas", [1080, 1920])),
                        tail_stop=spec.get("tail_stop"))
        rep = render(plan, args.master, args.out, bed=args.bed,
                     total_frames=int(spec["total_frames"]))
        return emit(envelope(True, rep), args.json,
                    f"{rep['out']}  register={rep['register']}  size={rep['size']}px")

    if args.cmd == "qa":
        from .qa import master_check
        rep = master_check(args.master, target_lufs=args.target_lufs,
                           flash=not args.no_flash)
        human = [f"{rep['size'][0]}x{rep['size'][1]} @ {rep['fps']}fps, "
                 f"{rep['duration']}s",
                 f"audio: {rep['audio']['loudness'].get('lufs')} LUFS, "
                 f"peak {rep['audio']['loudness'].get('true_peak')} dBFS, "
                 f"{rep['audio']['sample_rate']} Hz"]
        if "flash" in rep:
            human.append(f"flash: {len(rep['flash']['flashes'])} over threshold, "
                         f"max {rep['flash']['max_per_second']}/s (WCAG limit 3) -> "
                         f"{'PASS' if rep['flash']['ok'] else 'FAIL'}")
        human += ["  ! " + p for p in rep["problems"]]
        return emit(envelope(rep["ok"], rep, rep["problems"]), args.json, "\n".join(human))

    raise ToolError(f"unknown command {args.cmd}")


if __name__ == "__main__":                                     # pragma: no cover
    sys.exit(main())
