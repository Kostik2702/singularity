"""imgqa subcommands that leave the process: sheet (ffmpeg) and cutout
(dispatch into the isolated ML venv per OD-1). Also selftest.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

from ._contract import (SUBPROCESS_TIMEOUT_LONG, ContractError, out_name,
                        resolve_out)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}


def _run(cmd: list[str], timeout: int = SUBPROCESS_TIMEOUT_LONG) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise ContractError(f"executable not found: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ContractError(f"timeout after {timeout}s: {' '.join(cmd[:3])}...") from exc


def _ffprobe_duration(video: Path) -> float:
    proc = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "json", str(video)], timeout=60)
    if proc.returncode != 0:
        raise ContractError(f"ffprobe failed on {video.name}: {proc.stderr.strip()[:300]}")
    try:
        return float(json.loads(proc.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"ffprobe gave no duration for {video.name}") from exc


def _sheet_one(video: Path, out_dir: Path, cols: int, rows: int, width: int) -> dict:
    duration = _ffprobe_duration(video)
    frames = cols * rows
    fps = frames / max(duration, 0.001)
    # qualifier carries the source extension: clip.mp4 and clip.mov must not
    # collide on one clip.sheet.jpg (F9)
    dest = out_dir / out_name(video, "sheet", video.suffix.lstrip(".").lower(), "jpg")
    fd, tmp = tempfile.mkstemp(dir=str(out_dir), prefix=f".{dest.stem}.", suffix=".jpg")
    os.close(fd)  # unique per run — parallel runs can't clobber each other (F9)
    vf = f"fps={fps:.6f},scale={width}:-2,tile={cols}x{rows}"
    proc = _run(["ffmpeg", "-y", "-v", "error", "-i", str(video),
                 "-vf", vf, "-frames:v", "1", "-q:v", "3", tmp])
    if proc.returncode != 0 or not Path(tmp).stat().st_size:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise ContractError(f"ffmpeg sheet failed on {video.name}: {proc.stderr.strip()[:300]}")
    os.replace(tmp, dest)
    return {"video": str(video), "duration_s": round(duration, 2),
            "tile": f"{cols}x{rows}", "path": str(dest)}


def run_sheet(args):
    target = Path(args.target)
    if target.is_dir():
        videos = sorted(p for p in target.iterdir() if p.suffix.lower() in VIDEO_EXTS)
        if not videos:
            raise ContractError(f"no video files in {target}")
    elif target.is_file():
        videos = [target]
    else:
        raise ContractError(f"input not found: {target}")
    out_dir = resolve_out(args.out, videos)
    sheets = []
    try:
        for v in videos:
            sheets.append(_sheet_one(v, out_dir, args.cols, args.rows, args.width))
    except ContractError:
        # set-atomicity (F9): a failed directory run leaves no partial set behind
        for s in sheets:
            try:
                os.unlink(s["path"])
            except OSError:
                pass
        raise
    return ({"target": str(target.resolve()), "out": str(out_dir)},
            {"sheets": sheets}, None, [s["path"] for s in sheets])


# --- cutout dispatch (OD-1) ---------------------------------------------------

DEFAULT_ML_VENV = r"D:\venvs\imgqa-ml"


def _ml_python() -> Path:
    venv = os.environ.get("IMGQA_ML_VENV")
    if not venv:
        pointer = Path.home() / ".singularity.json"
        if pointer.is_file():
            try:
                venv = json.loads(pointer.read_text(encoding="utf-8-sig")).get("imgqa_ml_venv")
            except (json.JSONDecodeError, OSError):
                venv = None
    venv = venv or DEFAULT_ML_VENV
    py = Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not py.is_file():
        raise ContractError(
            f"ML venv python not found: {py}. Create the isolated venv (WSF-03 spike): "
            f"python -m venv {venv} && {venv}\\Scripts\\pip install rembg[cli] — "
            f"or point IMGQA_ML_VENV / ~/.singularity.json:imgqa_ml_venv at it. "
            f"Heavy ML deps must NEVER be installed into the engine venv (OD-1).")
    return py


def validate_cutout_payload(payload, out_dir: Path) -> list[str]:
    """Typed-boundary validation (F4, contract §4/§6): a boundary that accepts
    any exit-0 dict is not typed. Returns the verified output list."""
    if not isinstance(payload, dict):
        raise ContractError(f"ML-venv payload is not an object: {type(payload).__name__}")
    for field in ("schema_version", "status", "command", "outputs"):
        if field not in payload:
            raise ContractError(f"ML-venv payload missing required field {field!r}")
    if payload["status"] != "ok":
        raise ContractError(f"ML-venv reported status={payload['status']!r}: "
                            f"{payload.get('error', 'no error detail')}")
    if payload["command"] != "cutout":
        raise ContractError(f"ML-venv answered for command {payload['command']!r}, not cutout")
    outputs = payload["outputs"]
    if not isinstance(outputs, list) or not outputs:
        raise ContractError("ML-venv payload has no outputs — a cutout that "
                            "produced nothing is a tool error, not success")
    verified = []
    for entry in outputs:
        p = Path(entry).resolve()
        if not p.is_file():
            raise ContractError(f"ML-venv claims output {p} but the file does not exist")
        if out_dir.resolve() not in p.parents and p.parent != out_dir.resolve():
            raise ContractError(f"ML-venv output {p} escapes --out {out_dir}")
        verified.append(str(p))
    return verified


def run_cutout(args):
    path = Path(args.image)
    if not path.is_file():
        raise ContractError(f"input not found: {path}")
    out_dir = resolve_out(args.out, [path])
    py = _ml_python()
    proc = _run([str(py), "-m", "imgqa_ml.cutout", str(path),
                 "--out", str(out_dir), "--json"])
    if proc.returncode != 0:
        raise ContractError(
            f"ML-venv cutout failed (exit {proc.returncode}): {proc.stderr.strip()[:400]}")
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise ContractError("ML-venv cutout returned non-JSON output — treating as tool "
                            f"error, NEVER as a clean result: {proc.stdout[:200]!r}") from exc
    outputs = validate_cutout_payload(payload, out_dir)
    return ({"image": str(path.resolve()), "out": str(out_dir),
             "ml_python": str(py)},
            payload.get("data", {}), None, outputs)


# --- selftest -------------------------------------------------------------------

def _synthetic_cutout():
    from .synthetic import make_cutout
    return make_cutout()


def run_selftest(args):
    from types import SimpleNamespace

    import numpy as np
    from PIL import Image

    from . import ops
    from .fringe import run_detect_fringe
    checks = []

    def check(name: str, ok: bool, detail: str = ""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="imgqa-selftest-") as tmp:
        tmp_path = Path(tmp)
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        img = src_dir / "synthetic.png"
        Image.fromarray(_synthetic_cutout(), "RGBA").save(img)

        _, data, verdict, _ = ops.run_probe(SimpleNamespace(image=str(img)))
        check("probe.alpha", data["has_alpha"] and verdict["result"] == "PASS",
              f"verdict={verdict['result']}")

        out = tmp_path / "out"
        _, data, _, outputs = ops.run_edges(
            SimpleNamespace(image=str(img), out=str(out), max_regions=4))
        check("edges.writes", bool(outputs) and all(Path(p).is_file() for p in outputs),
              f"{len(outputs)} crops")
        check("edges.naming", all(Path(p).name.startswith("synthetic.edges.") for p in outputs))

        _, data, verdict, _ = ops.run_compare(SimpleNamespace(
            image_a=str(img), image_b=str(img), threshold=2, bbox=None,
            max_mae=1.0, max_diff_ratio=0.01))
        check("compare.identity", verdict["result"] == "PASS" and data["mae"] == 0.0)

        _, _, verdict, _ = ops.run_contrast(SimpleNamespace(fg="C9586B", bg="070506", min=4.5))
        check("contrast.gt", verdict["result"] == "PASS")

        _, data, verdict, _ = run_detect_fringe(SimpleNamespace(image=str(img)))
        halo_found = verdict["result"] == "FAIL" and len(data["detections"]) >= 1
        check("detect-fringe.halo-found", halo_found, f"{len(data['detections'])} detections")
        # strands live at x<150; the synthetic halo band starts at x>160 —
        # a detection intruding left of x=150 means hair-like structures were flagged
        strands_clear = all(d["bbox"][0] >= 150 for d in data["detections"])
        check("detect-fringe.strands-not-flagged", strands_clear,
              f"min x0 = {min((d['bbox'][0] for d in data['detections']), default='n/a')}")

        try:
            ops.run_edges(SimpleNamespace(image=str(img), out=str(src_dir), max_regions=2))
            check("out-policy.rejects-source-dir", False, "source dir was accepted")
        except ContractError:
            check("out-policy.rejects-source-dir", True)

    ok = all(c["ok"] for c in checks)
    if not ok:
        failed = ", ".join(c["check"] for c in checks if not c["ok"])
        raise ContractError(f"selftest failed: {failed} | detail: {json.dumps(checks)}")
    return {"shell": os.environ.get("SHELL") or os.environ.get("ComSpec", "?")}, \
        {"checks": checks}, None, None
