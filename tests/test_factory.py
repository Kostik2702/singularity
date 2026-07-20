"""factory status (WSF-E09 impl) — state is computed from artifacts."""
from __future__ import annotations

import json
from pathlib import Path

from singularity.factory.cli import compute_status, main as factory_main


def test_status_progresses_with_artifacts(tmp_path):
    project = tmp_path / "proj"
    (project / ".factory").mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()

    st = compute_status(project, evidence)
    assert st["next_action"].startswith("S1-moodboard")

    (evidence / "scout").mkdir()
    (evidence / "scout" / "moodboard.html").write_text("<html>", encoding="utf-8")
    (evidence / "design-brief-x.md").write_text("#", encoding="utf-8")
    st = compute_status(project, evidence)
    assert st["next_action"].startswith("S3-profile")
    assert st["pending_gate"] is None  # S3 gate only for profile CHANGE

    (project / ".factory" / "manifest.json").write_text("{}", encoding="utf-8")
    (evidence / "board").mkdir()
    (evidence / "board" / "review-board.html").write_text("<html>", encoding="utf-8")
    st = compute_status(project, evidence)
    assert st["next_action"].startswith("S5-intake")
    done = {s["stage"] for s in st["stages"] if s["done"]}
    assert {"S1-moodboard", "S2-design-brief", "S3-profile", "S4-shortlist"} <= done


def test_status_cli_real_matrix_evidence(capsys):
    """Live GT: the real MATRIX project + evidence tree computes a sane state."""
    project = Path(r"D:\Project\matrix-3d-web")
    evidence = Path(r"D:\Project\matrix-3d-web-evidence")
    if not (project.is_dir() and evidence.is_dir()):
        import pytest
        pytest.skip("MATRIX dogfood tree not present")
    rc = factory_main(["status", "--project", str(project),
                       "--evidence", str(evidence), "--json"])
    assert rc == 0
    env = json.loads(capsys.readouterr().out)
    done = {s["stage"] for s in env["data"]["stages"] if s["done"]}
    # the whole chain ran this session; baseline stays with the owner
    assert {"S1-moodboard", "S4-shortlist", "S5-intake",
            "S6-recipe", "S8-qa"} <= done
    assert env["data"]["golden_registry"], "golden registry should list items"
