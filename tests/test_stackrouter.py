"""stack router (WSF-E01) — the negative rules ARE the product (decision §2):
no React for a lone decorative effect, 3D only for real 3D needs,
next-content never by default, existing-project change = owner gate."""
from __future__ import annotations

import pytest

from singularity.providers.stackrouter import (ManifestError, recommend,
                                               validate_manifest)


def test_isolated_effect_never_pulls_react():
    rec = recommend({"content_site": True, "isolated_effects": 1})
    assert rec["profile"] == "static-lite"
    assert any("never adopted for a lone effect" in r for r in rec["reasons"])


def test_2d_canvas_effect_is_not_3d():
    # a code-rain 2D canvas is an isolated effect, not a scene-graph need
    rec = recommend({"content_site": True, "isolated_effects": 2,
                     "stateful_ui_components": 1})
    assert rec["profile"] == "static-lite"


def test_react_motion_needs_pressure():
    rec = recommend({"stateful_ui_components": 4,
                     "shared_motion_primitives": True})
    assert rec["profile"] == "react-motion"


def test_react_3d_requires_real_3d():
    rec = recommend({"needs_scene_graph": True, "needs_camera_paths": True})
    assert rec["profile"] == "react-3d"
    assert any("lazy island" in c for c in rec["constraints"])
    assert recommend({"stateful_ui_components": 5})["profile"] != "react-3d"


def test_next_content_only_on_explicit_needs():
    assert recommend({"content_site": True})["profile"] == "static-lite"
    rec = recommend({"content_site": True, "cms_required": True})
    assert rec["profile"] == "next-content"
    assert any("never a default" in r for r in rec["reasons"])


def test_existing_project_change_is_owner_gate():
    rec = recommend({"current_profile": "static-lite",
                     "needs_scene_graph": True})
    assert rec["profile"] == "react-3d"
    assert rec["owner_gate"] and "OWNER" in rec["owner_gate"]
    same = recommend({"current_profile": "react-3d",
                      "needs_scene_graph": True})
    assert same["owner_gate"] is None


def test_manifest_validation_rejects_typos():
    with pytest.raises(ManifestError):
        validate_manifest({"needs_scene_grpah": True})
    with pytest.raises(ManifestError):
        validate_manifest({"stateful_ui_components": -1})
    with pytest.raises(ManifestError):
        validate_manifest({"current_profile": "vue-lite"})
