"""video-report — deterministic technical evidence for video review (WSF-04).

Wraps ffprobe (metadata) and `imgqa sheet` (contact sheets) into one typed,
idempotent evidence run. INFORMATIONAL tool: it never judges crop vs
letterbox or content structure — that interpretation is the skill's
LLM/human step, and content decisions belong to the owner (NIKA-03 lesson).

Envelope: {"schema": "video-review/1", ...}; exit 0 = report generated,
exit 2 = tool error. Outputs in --out (deterministic names, atomic writes):
  report.json   — typed per-file metadata (no timestamps -> rerun byte-stable)
  report.txt    — GT-comparable text (same shape as reports/video-review/
                  ffprobe-report.txt in the NIKA tree)
  *.sheet.jpg   — contact sheets via imgqa (schema_version 2 pinned)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCHEMA = "video-review/1"
try:  # single source of truth with imgqa sheet (review F5); fallback = same set
    from singularity.imgqa.external import VIDEO_EXTS
except ImportError:
    VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".webm", ".mkv", ".avi"}
FFPROBE_TIMEOUT = 120
SHEET_TIMEOUT = 600  # large HEVC (375MB NIKA masters) must fit
IMGQA_SCHEMA = 2


class ToolError(Exception):
    pass


def atomic_write(text: str, dest: Path) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def resolve_out(out: str | None, inputs: list[Path]) -> Path:
    if not out:
        raise ToolError("--out is required")
    out_dir = Path(out).resolve()
    for src in inputs:
        src_dir = (src if src.is_dir() else src.parent).resolve()
        if out_dir == src_dir or out_dir in src_dir.parents or src_dir in out_dir.parents:
            raise ToolError(
                f"--out {out_dir} is inside/above the input tree of {src} — "
                f"in-place writes are forbidden")
    for root in filter(None, os.environ.get("IMGQA_PROTECTED_ROOTS", "").split(os.pathsep)):
        protected = Path(root).resolve()
        if out_dir == protected or protected in out_dir.parents:
            raise ToolError(f"--out {out_dir} is inside protected root {protected}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def list_videos(target: Path) -> list[Path]:
    if target.is_file():
        if target.suffix.lower() not in VIDEO_EXTS:
            raise ToolError(f"{target} is not a recognized video ({sorted(VIDEO_EXTS)})")
        return [target]
    if target.is_dir():
        videos = sorted(p for p in target.iterdir()
                        if p.is_file() and p.suffix.lower() in VIDEO_EXTS)
        if not videos:
            raise ToolError(f"no video files in {target}")
        return videos
    raise ToolError(f"input not found: {target}")


def ffprobe(path: Path) -> dict:
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", timeout=FFPROBE_TIMEOUT)
    except FileNotFoundError as exc:
        raise ToolError("ffprobe not found on PATH — install ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"ffprobe timeout on {path}") from exc
    if proc.returncode != 0:
        raise ToolError(f"ffprobe failed on {path}: {proc.stderr.strip()[:300]}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise ToolError(f"ffprobe emitted non-JSON for {path}") from exc


def _fps(rate: str | None) -> float | None:
    if not rate or "/" not in rate:
        return None
    num, _, den = rate.partition("/")
    try:
        num_f, den_f = float(num), float(den)
    except ValueError:
        return None
    return round(num_f / den_f, 3) if den_f else None


def _int_or_none(val) -> int | None:
    """ffprobe omits or N/A-s unknown numerics; 0 would silently conflate
    'unknown' with a literal zero (review F6)."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def summarize(path: Path, probe: dict) -> dict:
    fmt = probe.get("format", {})
    video = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    audio = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    try:
        duration = round(float(fmt.get("duration")), 3)
    except (TypeError, ValueError):
        duration = None
    entry = {
        "file": path.name,
        "path": str(path.resolve()),
        "size_bytes": _int_or_none(fmt.get("size")),
        "duration_s": duration,
        "container_bit_rate": _int_or_none(fmt.get("bit_rate")),
        "video": None,
        "audio": [],
        "warnings": [],
    }
    if video:
        v = video[0]
        # review F1: phone footage is often VFR, where r_frame_rate is the
        # container tick, not the shooting rate — prefer avg_frame_rate.
        r_fps = _fps(v.get("r_frame_rate"))
        avg_fps = _fps(v.get("avg_frame_rate"))
        if r_fps and avg_fps and abs(r_fps - avg_fps) > 0.01:
            entry["warnings"].append(
                f"{path.name}: VFR — r_frame_rate {r_fps} != avg {avg_fps}, "
                f"reporting avg")
        rotation = next((int(sd.get("rotation", 0) or 0)
                         for sd in v.get("side_data_list", [])
                         if "rotation" in sd), None)
        if rotation is None:  # ffprobe 4.x era exposes tags.rotate (review F7)
            rotation = _int_or_none((v.get("tags") or {}).get("rotate")) or 0
        entry["video"] = {
            "codec": v.get("codec_name"),
            "width": v.get("width"),
            "height": v.get("height"),
            "pix_fmt": v.get("pix_fmt"),
            "fps": avg_fps or r_fps,
            "bit_rate": _int_or_none(v.get("bit_rate")),
            "color_space": v.get("color_space"),
            "color_transfer": v.get("color_transfer"),
            "color_primaries": v.get("color_primaries"),
            "rotation": rotation,
        }
        if len(video) > 1:
            entry["warnings"].append(f"{len(video)} video streams; reporting first")
    else:
        entry["warnings"].append("no video stream")
    for a in audio:
        entry["audio"].append({"codec": a.get("codec_name"),
                               "bit_rate": _int_or_none(a.get("bit_rate"))})
    return entry


def gt_text(entries: list[dict], base: Path) -> str:
    """Same field set as the NIKA reference reports/video-review/
    ffprobe-report.txt — eyeball/diff-friendly for GT comparison, not
    byte-identical to it (fps is computed, durations 3dp)."""
    blocks = []
    base = base.resolve()
    for e in entries:
        try:
            rel = Path(e["path"]).relative_to(base)
            shown = f"{base.name}/{rel}".replace("\\", "/")
        except ValueError:
            shown = e["file"]
        def _s(val):  # unknown numerics print as N/A, matching raw ffprobe text
            return "N/A" if val is None else val

        lines = [f"=== {shown} ==="]
        v = e["video"]
        if v:
            lines += ["[STREAM]", f"codec_name={v['codec']}", "codec_type=video",
                      f"width={v['width']}", f"height={v['height']}",
                      f"pix_fmt={v['pix_fmt']}", f"color_space={v['color_space']}",
                      f"color_transfer={v['color_transfer']}",
                      f"color_primaries={v['color_primaries']}",
                      f"fps={_s(v['fps'])}", f"bit_rate={_s(v['bit_rate'])}",
                      "[/STREAM]"]
        for a in e["audio"]:
            lines += ["[STREAM]", f"codec_name={a['codec']}", "codec_type=audio",
                      f"bit_rate={_s(a['bit_rate'])}", "[/STREAM]"]
        lines += ["[FORMAT]", f"duration={_s(e['duration_s'])}",
                  f"size={_s(e['size_bytes'])}",
                  f"bit_rate={_s(e['container_bit_rate'])}", "[/FORMAT]", ""]
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def run_sheets(videos: list[Path], out_dir: Path, cols: int, rows: int,
               width: int) -> tuple[list[str], list[dict]]:
    """Contact sheets through imgqa (per file: one bad video must not void the
    others). Pin imgqa schema_version; a sheet failure is a typed warning."""
    outputs: list[str] = []
    problems: list[dict] = []
    for v in videos:
        cmd = [sys.executable, "-m", "singularity.imgqa.cli", "sheet", str(v),
               "--out", str(out_dir), "--cols", str(cols), "--rows", str(rows),
               "--width", str(width), "--json"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", timeout=SHEET_TIMEOUT)
            env = json.loads(proc.stdout)
        except subprocess.TimeoutExpired:
            problems.append({"file": v.name, "error": "sheet-timeout"})
            continue
        except ValueError:
            problems.append({"file": v.name, "error": "sheet-non-json-output"})
            continue
        if env.get("schema_version") != IMGQA_SCHEMA:
            problems.append({"file": v.name,
                             "error": f"imgqa schema {env.get('schema_version')} != {IMGQA_SCHEMA}"})
            continue
        if proc.returncode != 0 or env.get("status") != "ok":
            problems.append({"file": v.name,
                             "error": (env.get("error") or "sheet-failed")[:200]})
            continue
        outputs.extend(env.get("outputs", []))
    return outputs, problems


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(prog="video-report", description=__doc__)
    parser.add_argument("--input", required=True, help="video file or directory")
    parser.add_argument("--out", required=True)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-sheets", action="store_true",
                        help="metadata only (skip imgqa contact sheets)")
    parser.add_argument("--cols", type=int, default=4)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--width", type=int, default=320)
    args = parser.parse_args(argv)

    envelope: dict = {"schema": SCHEMA, "command": "report",
                      "inputs": {"input": args.input, "out": args.out,
                                 "sheets": not args.no_sheets}}
    try:
        target = Path(args.input)
        videos = list_videos(target)
        out_dir = resolve_out(args.out, videos)
        run_warnings: list[str] = []
        if target.is_dir():
            subdirs = [d.name for d in target.iterdir() if d.is_dir()]
            if subdirs:  # review F2: non-recursive listing must say so
                run_warnings.append(
                    f"{len(subdirs)} subdirectories not scanned "
                    f"(non-recursive): {', '.join(sorted(subdirs)[:5])}")
        # review F3, same principle as the scout adapter isolation ratified for
        # the epic: one unreadable file must not void the whole inventory.
        entries = []
        probe_problems: list[dict] = []
        for v in videos:
            try:
                entries.append(summarize(v, ffprobe(v)))
            except ToolError as exc:
                if "not found on PATH" in str(exc):
                    raise  # environment failure, not a file failure
                probe_problems.append({"file": v.name, "error": str(exc)[:300]})
        if not entries:
            raise ToolError(
                "no file could be probed: "
                + "; ".join(p["error"] for p in probe_problems[:3]))
        videos = [v for v in videos
                  if not any(p["file"] == v.name for p in probe_problems)]
        base = target if target.is_dir() else target.parent
        report_json = out_dir / "report.json"
        atomic_write(json.dumps({"schema": SCHEMA, "files": entries},
                                ensure_ascii=False, indent=1), report_json)
        report_txt = out_dir / "report.txt"
        atomic_write(gt_text(entries, base), report_txt)
        outputs = [str(report_json), str(report_txt)]
        sheet_problems: list[dict] = []
        if not args.no_sheets:
            sheet_outputs, sheet_problems = run_sheets(
                videos, out_dir, args.cols, args.rows, args.width)
            outputs += sheet_outputs
        envelope.update(status="ok",
                        data={"files": len(entries),
                              "warnings": run_warnings + [w for e in entries
                                                          for w in e["warnings"]],
                              "probe_problems": probe_problems,
                              "sheet_problems": sheet_problems},
                        outputs=outputs)
        rc = 0
    except ToolError as exc:
        envelope.update(status="error", error=str(exc))
        rc = 2
    except Exception as exc:  # noqa: BLE001 — typed envelope even on surprises
        envelope.update(status="error",
                        error=f"unexpected {type(exc).__name__}: {exc}")
        rc = 2
    if args.json:
        print(json.dumps(envelope, ensure_ascii=False))
    else:
        line = f"[video-report] {envelope['status']}"
        if envelope.get("outputs"):
            line += f" | outputs: {len(envelope['outputs'])}"
        if envelope.get("error"):
            line += f" | error: {envelope['error']}"
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main())
