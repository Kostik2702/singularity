"""Shared plumbing: ffmpeg/ffprobe invocation, envelopes, raw-frame streaming."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator

from . import ENVELOPE


class ToolError(RuntimeError):
    """The tool could not do its job. Never means 'the video is bad'."""


def require_ffmpeg() -> None:
    for exe in ("ffmpeg", "ffprobe"):
        if shutil.which(exe) is None:
            raise ToolError(f"{exe} not found on PATH")


# A filter graph can buffer without bound — chained `xfade` over `zoompan`
# output was measured at ~100 GB RSS here and took the machine down. No render
# is worth that, so every child process is watched and killed at the ceiling.
MEM_LIMIT_GB = float(os.environ.get("REEL_MEM_LIMIT_GB", "8"))


def _rss_gb(pid: int) -> float:
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                                    # pragma: no cover
        return 0.0
    if os.name != "nt":                                  # pragma: no cover
        try:
            with open(f"/proc/{pid}/statm") as fh:
                return int(fh.read().split()[1]) * 4096 / 2 ** 30
        except OSError:
            return 0.0

    class _PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]
    h = ctypes.windll.kernel32.OpenProcess(0x0400 | 0x0010, False, pid)
    if not h:
        return 0.0
    try:
        c = _PMC()
        c.cb = ctypes.sizeof(_PMC)
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return c.WorkingSetSize / 2 ** 30
    finally:
        ctypes.windll.kernel32.CloseHandle(h)
    return 0.0


def run(cmd: list[str], *, capture: bool = True, mem_limit_gb: float | None = None) -> bytes:
    """Run a child, watching its memory. Pipes are drained in a THREAD.

    Draining matters more than it looks: polling the process while leaving
    stdout/stderr as unread pipes deadlocks the child as soon as it writes 64 KB
    — it blocks on the write, the poll loop waits for an exit that can never
    come, and a render that takes 0.6 s appears to hang forever. `subprocess.run`
    hides this by reading concurrently; a hand-rolled watchdog must do the same.
    """
    import threading

    limit = MEM_LIMIT_GB if mem_limit_gb is None else mem_limit_gb
    p = subprocess.Popen(cmd,
                         stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.PIPE if capture else None)
    box: dict[str, bytes] = {}

    def _drain() -> None:
        box["out"], box["err"] = p.communicate()

    reader = threading.Thread(target=_drain, daemon=True)
    reader.start()

    peak = 0.0
    killed = False
    while reader.is_alive():
        peak = max(peak, _rss_gb(p.pid))
        if limit > 0 and peak > limit:
            p.kill()
            killed = True
            break
        time.sleep(0.4)
    reader.join(timeout=30)
    out, err = box.get("out"), box.get("err")
    if killed:
        raise ToolError(
            f"{cmd[0]} убит сторожем памяти: {peak:.1f} ГБ > лимита {limit:g} ГБ. "
            "Так ведёт себя буферизующий граф (сцепленный xfade, fps после "
            "zoompan). Упростить граф, а не поднимать лимит.")
    if p.returncode:
        tail = (err or b"")[-800:].decode("utf-8", "replace")
        raise ToolError(f"{cmd[0]} failed ({p.returncode}): {tail}")
    return out or b""


def probe_json(path: str | Path) -> dict[str, Any]:
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_format", "-show_streams", str(path)])
    return json.loads(out.decode("utf-8", "replace"))


def frames(path: str | Path, w: int, h: int, pix: str = "rgb24") -> Iterator[bytes]:
    """Stream decoded frames as raw bytes. Nothing is written to disk."""
    bpp = {"rgb24": 3, "rgba": 4, "gray": 1}[pix]
    n = w * h * bpp
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(path),
                          "-f", "rawvideo", "-pix_fmt", pix, "-"],
                         stdout=subprocess.PIPE)
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                return
            yield buf
    finally:
        if p.poll() is None:
            p.kill()
        p.wait()


def pcm_mono(path: str | Path, sr: int = 22050):
    """Decode to mono float32 in [-1, 1]. numpy is imported lazily so that the
    metadata-only subcommands stay usable on a host without it."""
    import numpy as np
    raw = run(["ffmpeg", "-v", "error", "-i", str(path), "-ac", "1",
               "-ar", str(sr), "-f", "s16le", "-"])
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def loudness(path: str | Path) -> dict[str, float]:
    """EBU R128 integrated loudness / range / true peak."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                        "-af", "ebur128=peak=true", "-f", "null", "-"],
                       capture_output=True)
    text = (p.stderr or b"").decode("utf-8", "replace")
    out: dict[str, float] = {}
    for key, label in (("lufs", "I:"), ("lra", "LRA:"), ("true_peak", "Peak:")):
        hits = [ln for ln in text.splitlines() if label in ln]
        if hits:
            try:
                out[key] = float(hits[-1].split(label)[1].split()[0])
            except (IndexError, ValueError):
                pass
    return out


def envelope(ok: bool, data: dict[str, Any], problems: list[str] | None = None) -> dict[str, Any]:
    return {"schema": ENVELOPE, "ok": bool(ok), "data": data, "problems": problems or []}


def emit(env: dict[str, Any], as_json: bool, human: str = "") -> int:
    """Print the envelope (or a human summary) and give the process exit code.

    Exit 2 means the TOOL failed, never 'the material is bad' — same contract as
    video-visual-review, so callers can tell the two apart."""
    if as_json:
        json.dump(env, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    elif human:
        print(human)
    return 0 if env["ok"] else 2
