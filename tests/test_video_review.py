"""video-visual-review (WSF-04) — contract tests for the video-report script.

Auto-skips when ffmpeg/ffprobe are absent (same opt-in philosophy as the
gemini_e2e marker); on the dev machine both are present so the suite runs."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
SCRIPT = ENGINE / "singularity" / "skills" / "video-visual-review" / "scripts" / "video-report.py"

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
    reason="ffmpeg/ffprobe not on PATH")


def _run(*argv: str) -> tuple[int, dict]:
    # harness timeout must exceed the child's SHEET_TIMEOUT=600 (review F9)
    proc = subprocess.run([sys.executable, str(SCRIPT), *argv, "--json"],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=660)
    return proc.returncode, json.loads(proc.stdout)


def _load_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("video_report", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def video_dir(tmp_path_factory) -> Path:
    src = tmp_path_factory.mktemp("vidsrc")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=320x240:rate=10",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src / "synthetic.mp4")],
        check=True, timeout=120)
    (src / "not-a-video.txt").write_text("ignore me", encoding="utf-8")
    return src


def test_report_fields_and_outputs(video_dir, tmp_path):
    rc, env = _run("--input", str(video_dir / "synthetic.mp4"),
                   "--out", str(tmp_path / "out"))
    assert rc == 0 and env["status"] == "ok" and env["schema"] == "video-review/1"
    report = json.loads((tmp_path / "out" / "report.json").read_text("utf-8"))
    entry = report["files"][0]
    assert entry["video"]["codec"] == "h264"
    assert (entry["video"]["width"], entry["video"]["height"]) == (320, 240)
    assert entry["video"]["fps"] == 10.0
    assert 1.5 < entry["duration_s"] < 2.5
    txt = (tmp_path / "out" / "report.txt").read_text("utf-8")
    assert txt.startswith("===") and "codec_name=h264" in txt
    sheets = list((tmp_path / "out").glob("*.sheet.*.jpg"))
    assert len(sheets) == 1, env["outputs"]


def test_directory_input_skips_non_videos(video_dir, tmp_path):
    rc, env = _run("--input", str(video_dir), "--out", str(tmp_path / "out"),
                   "--no-sheets")
    assert rc == 0
    report = json.loads((tmp_path / "out" / "report.json").read_text("utf-8"))
    assert [f["file"] for f in report["files"]] == ["synthetic.mp4"]


def test_idempotent_rerun_byte_stable(video_dir, tmp_path):
    out = tmp_path / "out"
    _run("--input", str(video_dir), "--out", str(out), "--no-sheets")
    first_json = (out / "report.json").read_bytes()
    first_txt = (out / "report.txt").read_bytes()
    rc, _ = _run("--input", str(video_dir), "--out", str(out), "--no-sheets")
    assert rc == 0
    assert (out / "report.json").read_bytes() == first_json
    assert (out / "report.txt").read_bytes() == first_txt


def test_out_inside_input_tree_refused(video_dir):
    rc, env = _run("--input", str(video_dir), "--out", str(video_dir / "out"))
    assert rc == 2 and env["status"] == "error"
    assert "forbidden" in env["error"]


def test_missing_input_is_tool_error(tmp_path):
    rc, env = _run("--input", str(tmp_path / "nope.mp4"),
                   "--out", str(tmp_path / "out"))
    assert rc == 2 and env["status"] == "error"


def test_empty_dir_is_tool_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    rc, env = _run("--input", str(empty), "--out", str(tmp_path / "out"))
    assert rc == 2 and "no video files" in env["error"]


def test_vfr_prefers_avg_frame_rate_with_warning():
    mod = _load_module()
    probe = {"format": {"size": "10", "duration": "2.0", "bit_rate": "100"},
             "streams": [{"codec_type": "video", "codec_name": "h264",
                          "width": 640, "height": 480, "pix_fmt": "yuv420p",
                          "r_frame_rate": "600/1",
                          "avg_frame_rate": "30000/1001"}]}
    entry = mod.summarize(Path("vfr.mp4"), probe)
    assert entry["video"]["fps"] == 29.97
    assert any("VFR" in w for w in entry["warnings"])


def test_unknown_bitrate_is_none_not_zero():
    mod = _load_module()
    probe = {"format": {"duration": "2.0"},
             "streams": [{"codec_type": "video", "codec_name": "vp9",
                          "width": 640, "height": 480,
                          "r_frame_rate": "30/1", "avg_frame_rate": "30/1"}]}
    entry = mod.summarize(Path("x.webm"), probe)
    assert entry["video"]["bit_rate"] is None
    assert entry["container_bit_rate"] is None
    txt = mod.gt_text([entry], Path("."))
    assert "bit_rate=N/A" in txt and "bit_rate=0" not in txt


def test_corrupt_file_isolated_not_fatal(video_dir, tmp_path):
    import shutil as sh
    mixed = tmp_path / "mixed"
    mixed.mkdir()
    sh.copy(video_dir / "synthetic.mp4", mixed / "good.mp4")
    (mixed / "corrupt.mp4").write_bytes(b"this is not a video")
    rc, env = _run("--input", str(mixed), "--out", str(tmp_path / "out"),
                   "--no-sheets")
    assert rc == 0, env
    assert [p["file"] for p in env["data"]["probe_problems"]] == ["corrupt.mp4"]
    report = json.loads((tmp_path / "out" / "report.json").read_text("utf-8"))
    assert [f["file"] for f in report["files"]] == ["good.mp4"]


def test_all_corrupt_is_tool_error(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "corrupt.mp4").write_bytes(b"junk")
    rc, env = _run("--input", str(bad), "--out", str(tmp_path / "out"))
    assert rc == 2 and "no file could be probed" in env["error"]


def test_subdirectories_reported_as_warning(video_dir, tmp_path):
    import shutil as sh
    root = tmp_path / "shoot"
    (root / "sub").mkdir(parents=True)
    sh.copy(video_dir / "synthetic.mp4", root / "top.mp4")
    sh.copy(video_dir / "synthetic.mp4", root / "sub" / "nested.mp4")
    rc, env = _run("--input", str(root), "--out", str(tmp_path / "out"),
                   "--no-sheets")
    assert rc == 0
    assert any("subdirectories not scanned" in w for w in env["data"]["warnings"])


def test_protected_root_refused(video_dir, tmp_path, monkeypatch):
    guarded = tmp_path / "site"
    monkeypatch.setenv("IMGQA_PROTECTED_ROOTS", str(guarded))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--input", str(video_dir),
         "--out", str(guarded / "ev"), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=120)
    env = json.loads(proc.stdout)
    assert proc.returncode == 2 and "protected root" in env["error"]
