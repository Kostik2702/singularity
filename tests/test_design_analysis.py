"""design-analysis (WSF-06) — contract tests for design-metrics.py.
Needs Pillow ([imgqa] extras) — present in the engine venv."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PIL = pytest.importorskip("PIL.Image")

ENGINE = Path(__file__).resolve().parents[1]
SCRIPT = ENGINE / "singularity" / "skills" / "design-analysis" / "scripts" / "design-metrics.py"


def _run(*argv: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, str(SCRIPT), *argv, "--json"],
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=120)
    return proc.returncode, json.loads(proc.stdout)


@pytest.fixture(scope="module")
def striped_mockup(tmp_path_factory) -> Path:
    """3 equal 300px bands: red / green / blue, 400 wide."""
    from PIL import Image
    im = Image.new("RGB", (400, 900))
    for y in range(900):
        color = (200, 30, 30) if y < 300 else (30, 180, 30) if y < 600 else (30, 30, 200)
        for x in range(400):
            im.putpixel((x, y), color)
    path = tmp_path_factory.mktemp("mock") / "striped.png"
    im.save(path)
    return path


def test_dims_bands_and_palette(striped_mockup, tmp_path):
    rc, env = _run(str(striped_mockup), "--out", str(tmp_path / "ev"))
    assert rc == 0 and env["status"] == "ok" and env["schema"] == "design-metrics/1"
    data = env["data"]
    assert (data["width"], data["height"]) == (400, 900)
    assert data["aspect_h_over_w"] == 2.25
    bands = data["bands"]
    assert len(bands) == 3, bands
    assert [b["top"] for b in bands] == [0, 300, 600]
    assert bands[0]["share_pct"] == pytest.approx(33.3, abs=0.2)
    top_colors = {c["hex"] for c in data["dominant_colors"][:3]}
    assert "#c81e1e" in top_colors  # red band color survives quantization
    metrics_file = tmp_path / "ev" / "striped.design-metrics.json"
    assert metrics_file.is_file()
    assert json.loads(metrics_file.read_text("utf-8"))["width"] == 400


def test_missing_input_is_tool_error(tmp_path):
    rc, env = _run(str(tmp_path / "nope.png"))
    assert rc == 2 and env["status"] == "error"


def test_out_inside_mockup_tree_refused(striped_mockup):
    rc, env = _run(str(striped_mockup), "--out", str(striped_mockup.parent / "ev"))
    assert rc == 2 and "forbidden" in env["error"]


def test_non_image_is_tool_error(tmp_path):
    bad = tmp_path / "fake.png"
    bad.write_bytes(b"not an image at all")
    rc, env = _run(str(bad))
    assert rc == 2 and "cannot decode" in env["error"]
