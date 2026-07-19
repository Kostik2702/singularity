"""golden registry (WSF-E07 part 1) — the door checks and the promotion
gate ARE the product: no license / incomplete recipe never enters; candidate
becomes golden only via second application or explicit owner decision;
install is a provider-independent offline copy."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

from singularity.golden.cli import main as golden_main


def _run(*argv: str) -> tuple[int, dict]:
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = golden_main([*argv, "--json"])
    finally:
        sys.stdout = old
    return rc, json.loads(buf.getvalue())


def _complete_recipe(tmp_path: Path, *, license_name="MIT") -> Path:
    recipe = {"schema_version": "adaptation-recipe/1"}
    from singularity.intake.recipe import FIELDS
    for field in FIELDS:
        recipe[field] = f"resolved: {field}"
    recipe["upstream"] = {"origin": "https://up.example/x.json",
                          "retrieved_at": "2026-07-19T00:00:00+00:00",
                          "files": [{"path": "Fx.jsx", "sha256": "ab" * 32}]}
    recipe["license"] = {"license": license_name, "source": "record",
                         "obligations": "none"}
    recipe["intake_verdict"] = "ADAPT"
    p = tmp_path / "recipe.json"
    p.write_text(json.dumps(recipe), encoding="utf-8")
    return p


@pytest.fixture()
def registry(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "registry"
    monkeypatch.setenv("GOLDEN_REGISTRY_DIR", str(root))
    return root


@pytest.fixture()
def adapted_dir(tmp_path) -> Path:
    d = tmp_path / "adapted"
    d.mkdir()
    (d / "Fx.jsx").write_text("export default () => null\n", encoding="utf-8")
    (d / "fx.css").write_text(".x{opacity:1}\n", encoding="utf-8")
    return d


def test_add_and_lifecycle(registry, adapted_dir, tmp_path):
    recipe = _complete_recipe(tmp_path)
    rc, env = _run("add", "--name", "glitch-bg", "--recipe", str(recipe),
                   "--files", str(adapted_dir), "--capability", "background",
                   "--stack", "static-lite", "--project", "matrix-3d-web",
                   "--evidence", "evidence/shot.png")
    assert rc == 0 and env["data"]["status"] == "candidate"

    # search by capability finds it; wrong capability doesn't
    rc, env = _run("search", "--capability", "background")
    assert env["data"]["count"] == 1
    rc, env = _run("search", "--capability", "shader")
    assert env["data"]["count"] == 0

    # promotion without evidence refused
    rc, env = _run("promote", "--name", "glitch-bg")
    assert rc == 2 and "promotion gate" in env["error"]

    # second application promotes
    rc, env = _run("promote", "--name", "glitch-bg",
                   "--reuse-evidence", "evidence/second-run.png",
                   "--project", "nika")
    assert rc == 0 and env["data"]["status"] == "golden"

    # offline install copies files, no provider anywhere
    dest = tmp_path / "target-project"
    rc, env = _run("install", "--name", "glitch-bg", "--dest", str(dest))
    assert rc == 0 and (dest / "Fx.jsx").is_file() and (dest / "fx.css").is_file()
    assert "no provider involved" in env["data"]["note"]


def test_incomplete_recipe_refused_at_the_door(registry, adapted_dir, tmp_path):
    from singularity.intake.recipe import FIELDS
    recipe = {"schema_version": "adaptation-recipe/1"}
    for field in FIELDS:
        recipe[field] = f"TODO: {field}"
    p = tmp_path / "draft.json"
    p.write_text(json.dumps(recipe), encoding="utf-8")
    rc, env = _run("add", "--name", "draft-item", "--recipe", str(p),
                   "--files", str(adapted_dir))
    assert rc == 2 and "recipe incomplete" in env["error"]
    rc, env = _run("list")
    assert env["data"]["count"] == 0


def test_unknown_license_refused(registry, adapted_dir, tmp_path):
    recipe = _complete_recipe(tmp_path, license_name="unknown")
    rc, env = _run("add", "--name", "shady", "--recipe", str(recipe),
                   "--files", str(adapted_dir))
    assert rc == 2 and "license" in env["error"]


def test_owner_decision_promotes_single_application(registry, adapted_dir, tmp_path):
    recipe = _complete_recipe(tmp_path)
    _run("add", "--name", "hero-stage", "--recipe", str(recipe),
         "--files", str(adapted_dir), "--capability", "hero")
    rc, env = _run("promote", "--name", "hero-stage",
                   "--owner-decision", "ratified 2026-07-19 (vault decision X)")
    assert rc == 0 and env["data"]["promotion"]["by"] == "owner-decision"


def test_duplicate_name_refused(registry, adapted_dir, tmp_path):
    recipe = _complete_recipe(tmp_path)
    _run("add", "--name", "dup", "--recipe", str(recipe),
         "--files", str(adapted_dir))
    rc, env = _run("add", "--name", "dup", "--recipe", str(recipe),
                   "--files", str(adapted_dir))
    assert rc == 2 and "already exists" in env["error"]
