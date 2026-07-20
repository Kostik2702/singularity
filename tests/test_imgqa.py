"""imgqa GT suite (WSF-01d, CONTRACT.md §5).

Tier 1 — synthetic fixtures, generated on the fly (no binaries).
Tier 2 — committed real NIKA before/after crops (tests/fixtures/imgqa/).
Tier 3 — full-image GT against the live NIKA tree; marker `imgqa_gt`,
         auto-skips unless IMGQA_GT_ROOT points at the NIKA project root.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "imgqa"
GT_ROOT = os.environ.get("IMGQA_GT_ROOT")

requires_gt_root = pytest.mark.imgqa_gt


def run_imgqa(*argv: str) -> tuple[int, dict]:
    proc = subprocess.run([sys.executable, "-m", "singularity.imgqa.cli",
                           *argv, "--json"],
                          capture_output=True, text=True, timeout=300)
    payload = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    return proc.returncode, payload


# --- tier 1: synthetic ---------------------------------------------------------

def _write_synthetic(tmp_path: Path, name: str, arr) -> Path:
    from PIL import Image
    mode = "RGBA" if arr.shape[-1] == 4 else "RGB"
    dest = tmp_path / name
    Image.fromarray(arr, mode).save(dest)
    return dest


def test_selftest_green():
    code, env = run_imgqa("selftest")
    assert code == 0, env.get("error")
    assert all(c["ok"] for c in env["data"]["checks"])


def test_probe_checkerboard_gt_pr2(tmp_path):
    from singularity.imgqa.synthetic import make_checkerboard
    img = _write_synthetic(tmp_path / "..", "board.png", make_checkerboard())
    code, env = run_imgqa("probe", str(img))
    assert env["data"]["baked_checkerboard"] is True
    assert env["verdict"]["result"] == "FAIL" and code == 1


def test_synthetic_detect_defringe_roundtrip(tmp_path):
    """End-to-end: halo detected -> defringe by detected bbox -> re-detect clean."""
    from singularity.imgqa.synthetic import make_cutout
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    img = _write_synthetic(src_dir, "cutout.png", make_cutout())
    code, env = run_imgqa("detect-fringe", str(img))
    assert code == 1 and len(env["data"]["detections"]) >= 1
    x, y, w, h = env["data"]["detections"][0]["bbox"]
    # NIKA lesson: the repair bbox must be GENEROUS (>=3x feather margin) or the
    # soft mask edge leaves a half-treated translucent band that re-detects
    feather, margin = 22, 3 * 22
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(256, x + w + margin), min(256, y + h + margin)  # clamp: bbox must be fully in-bounds
    x, y, w, h = x0, y0, x1 - x0, y1 - y0
    out = tmp_path / "out"
    code, env = run_imgqa("defringe", str(img), "--bbox", f"{x},{y},{w},{h}",
                          "--feather", str(feather), "--out", str(out))
    assert code == 0
    fixed = env["outputs"][0]
    code, env = run_imgqa("detect-fringe", fixed)
    assert code == 0, f"halo survived defringe: {env['data']['detections']}"


def test_exit2_is_never_pass(tmp_path):
    code, env = run_imgqa("probe", str(tmp_path / "does-not-exist.png"))
    assert code == 2 and env["status"] == "error" and "verdict" not in env


def test_out_policy_rejects_source_dir(tmp_path):
    from singularity.imgqa.synthetic import make_cutout
    img = _write_synthetic(tmp_path, "cutout.png", make_cutout())
    code, env = run_imgqa("edges", str(img), "--out", str(tmp_path))
    assert code == 2 and "in-place" in env["error"]


def test_contrast_gt_c1():
    code, env = run_imgqa("contrast", "C9586B", "070506")
    assert code == 0 and env["data"]["ratio"] == 4.93


def test_envelope_carries_version_evidence():
    _, env = run_imgqa("contrast", "FFFFFF", "000000")
    assert env["schema_version"] == 2
    for key in ("imgqa", "pillow", "numpy"):
        assert env["tool"][key]


# --- review regressions (Phase 4 findings, 2026-07-14) --------------------------

def test_argparse_error_keeps_json_contract():
    """F2: missing required --out must still emit a schema envelope on stdout."""
    proc = subprocess.run([sys.executable, "-m", "singularity.imgqa.cli",
                           "edges", "whatever.png", "--json"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2
    env = json.loads(proc.stdout.strip().splitlines()[-1])
    assert env["schema_version"] and env["status"] == "error"


def test_negative_bbox_is_tool_error():
    """F3: negative origin used to PASS via numpy end-indexing."""
    code, env = run_imgqa("compare", str(FIXTURES / "boot-fringe.before.png"),
                          str(FIXTURES / "boot-fringe.after.png"),
                          "--bbox=-10,0,5,5", "--max-mae", "1.0",
                          "--max-diff-ratio", "0.01")
    assert code == 2 and "verdict" not in env


def test_out_of_bounds_defringe_bbox_is_tool_error(tmp_path):
    """F3: fully-out-of-bounds bbox used to be a successful no-op."""
    code, env = run_imgqa("defringe", str(FIXTURES / "boot-fringe.before.png"),
                          "--bbox", "9999,9999,10,10", "--out", str(tmp_path))
    assert code == 2 and "exceeds image bounds" in env["error"]


def test_contrast_gates_on_raw_ratio():
    """F6: 777777/070707 is 4.4983 — must FAIL AA 4.5, not round up to a PASS."""
    code, env = run_imgqa("contrast", "777777", "070707")
    assert code == 1 and env["verdict"]["result"] == "FAIL"
    assert env["data"]["ratio"] == 4.5 and env["data"]["aa_normal"] is False


def test_cutout_payload_validation_rejects_fakes(tmp_path):
    """F4: an exit-0 empty dict from the ML venv must not count as success."""
    from singularity.imgqa._contract import ContractError
    from singularity.imgqa.external import validate_cutout_payload
    with pytest.raises(ContractError):
        validate_cutout_payload({}, tmp_path)
    with pytest.raises(ContractError):
        validate_cutout_payload({"schema_version": 1, "status": "ok",
                                 "command": "cutout", "outputs": []}, tmp_path)
    with pytest.raises(ContractError):  # claimed file doesn't exist
        validate_cutout_payload({"schema_version": 1, "status": "ok",
                                 "command": "cutout",
                                 "outputs": [str(tmp_path / "ghost.png")]}, tmp_path)
    real = tmp_path / "cut.png"
    real.write_bytes(b"x")
    escape = tmp_path.parent / "escape.png"
    escape.write_bytes(b"x")
    try:
        with pytest.raises(ContractError):  # outputs escaping --out
            validate_cutout_payload({"schema_version": 1, "status": "ok",
                                     "command": "cutout",
                                     "outputs": [str(escape)]}, tmp_path)
    finally:
        escape.unlink()
    assert validate_cutout_payload(
        {"schema_version": 1, "status": "ok", "command": "cutout",
         "outputs": [str(real)]}, tmp_path) == [str(real.resolve())]


def test_protected_roots_rejected(tmp_path, monkeypatch):
    """F8: IMGQA_PROTECTED_ROOTS puts the audited project off-limits for --out."""
    from singularity.imgqa.synthetic import make_cutout
    protected = tmp_path / "nika-project"
    (protected / "sub").mkdir(parents=True)
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    img = _write_synthetic(src_dir, "cutout.png", make_cutout())
    monkeypatch.setenv("IMGQA_PROTECTED_ROOTS", str(protected))
    code, env = run_imgqa("edges", str(img), "--out", str(protected / "sub"))
    assert code == 2 and "protected root" in env["error"]


# --- tier 2: committed real crops ---------------------------------------------

def test_crop_before_has_exactly_one_fringe():
    code, env = run_imgqa("detect-fringe", str(FIXTURES / "boot-fringe.before.png"))
    dets = env["data"]["detections"]
    assert code == 1 and len(dets) == 1 and dets[0]["score"] >= 0.8


def test_crop_after_is_clean_gt_n1_local():
    code, env = run_imgqa("detect-fringe", str(FIXTURES / "boot-fringe.after.png"))
    assert code == 0 and env["data"]["detections"] == []


def test_crop_defringe_reproduces_accepted_result_gt_d1_local(tmp_path):
    code, env = run_imgqa("defringe", str(FIXTURES / "boot-fringe.before.png"),
                          "--bbox", "0,0,680,428", "--feather", "22",
                          "--out", str(tmp_path))
    assert code == 0
    code, env = run_imgqa("compare", env["outputs"][0],
                          str(FIXTURES / "boot-fringe.after.png"),
                          "--bbox", "40,40,600,348",
                          "--max-mae", "1.0", "--max-diff-ratio", "0.01")
    assert code == 0, env["data"]


# --- tier 3: full-image GT (auto-skip without IMGQA_GT_ROOT) --------------------

SD3_SOURCE = "references/IMG GEN_SD3 - Remove Background_2026-07-13_05-34-49.png"
HERO_MASTER = "assets/images/hero/hero-master.png"
REPAIR_BBOX = (330, 1620, 680, 428)  # X,Y,W,H


def _gt(relpath: str) -> str:
    if not GT_ROOT:
        pytest.skip("IMGQA_GT_ROOT not set — full-image GT tier skipped")
    path = Path(GT_ROOT) / relpath
    if not path.is_file():
        # a CONFIGURED root with missing assets is a broken setup, not an
        # opt-out — skipping here made a wrong root look green (F5)
        pytest.fail(f"IMGQA_GT_ROOT is set but GT asset is missing: {path}")
    return str(path)


@requires_gt_root
def test_full_gt_p1_detection_inside_repair_region():
    code, env = run_imgqa("detect-fringe", _gt(SD3_SOURCE))
    dets = env["data"]["detections"]
    assert code == 1 and len(dets) == 1
    x, y, w, h = dets[0]["bbox"]
    rx, ry, rw, rh = REPAIR_BBOX
    ix = max(0, min(x + w, rx + rw) - max(x, rx))
    iy = max(0, min(y + h, ry + rh) - max(y, ry))
    assert ix * iy >= 0.9 * w * h, "detection not inside the repair region (GT-P1)"


@requires_gt_root
def test_full_gt_n1_clean_image_no_detections():
    code, env = run_imgqa("detect-fringe", _gt(HERO_MASTER))
    assert code == 0 and env["data"]["detections"] == []


@requires_gt_root
def test_full_gt_d1_defringe_reproduction(tmp_path):
    rx, ry, rw, rh = REPAIR_BBOX
    code, env = run_imgqa("defringe", _gt(SD3_SOURCE),
                          "--bbox", f"{rx},{ry},{rw},{rh}", "--feather", "22",
                          "--out", str(tmp_path))
    assert code == 0
    code, env = run_imgqa("compare", env["outputs"][0], _gt(HERO_MASTER),
                          "--bbox", f"{rx},{ry},{rw},{rh}",
                          "--max-mae", "1.0", "--max-diff-ratio", "0.01")
    assert code == 0, env["data"]


# --- cutout dispatch e2e (WSF-03; auto-skips without the ML venv) ---------------

ML_VENV = Path(os.environ.get("IMGQA_ML_VENV", r"D:\venvs\imgqa-ml"))
requires_ml_venv = pytest.mark.skipif(
    not (ML_VENV / "Scripts" / "python.exe").is_file()
    and not (ML_VENV / "bin" / "python").is_file(),
    reason="isolated ML venv not provisioned (WSF-03 spike)")


def test_cutout_missing_venv_is_tool_error_with_remediation(tmp_path, monkeypatch):
    monkeypatch.setenv("IMGQA_ML_VENV", str(tmp_path / "no-such-venv"))
    code, env = run_imgqa("cutout", str(FIXTURES / "boot-fringe.before.png"),
                          "--out", str(tmp_path / "out"))
    assert code == 2 and "ML venv python not found" in env["error"]
    assert "OD-1" in env["error"]  # remediation must explain the isolation rule


@requires_ml_venv
def test_cutout_dispatch_end_to_end(tmp_path):
    from singularity.imgqa.synthetic import make_cutout
    from PIL import Image
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = src_dir / "subject.png"
    # flatten the synthetic cutout onto a background -> a real removal task
    rgba = make_cutout()
    im = Image.fromarray(rgba, "RGBA")
    flat = Image.new("RGBA", im.size, (40, 90, 40, 255))
    flat.alpha_composite(im)
    flat.convert("RGB").save(src)
    out = tmp_path / "out"
    code, env = run_imgqa("cutout", str(src), "--out", str(out))
    assert code == 0, env.get("error")
    produced = Path(env["outputs"][0])
    assert produced.is_file() and produced.parent == out.resolve()
    code, env = run_imgqa("probe", str(produced))
    assert env["data"]["has_alpha"] is True
