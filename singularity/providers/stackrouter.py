"""Stack profile router (WSF-E01, decision §2): deterministic recommendation
of the CHEAPEST SUFFICIENT stack profile from a typed project manifest.

Profiles: static-lite < react-motion < react-3d; next-content is orthogonal
(entered only via explicit SSR/CMS/dynamic-routes needs, never by default).
The router explains every step; changing the profile of an EXISTING project
is always an owner gate — the router only recommends.

Hard rules encoded from the ratified decision:
- React is never chosen for one isolated decorative effect (a lone canvas
  background on a content site stays static-lite with a JS island).
- react-3d requires REAL 3D needs: scene graph / camera work / 3D
  materials / 3D interactivity — a 2D canvas effect is not 3D.
- next-content requires explicit SSR / CMS / dynamic-routes demand from the
  brief contract; "might need it later" does not count.
"""
from __future__ import annotations

PROFILES = ("static-lite", "react-motion", "react-3d", "next-content")

# manifest fields (all optional, absent == False/empty):
#   content_site: bool          — marketing/content page set
#   ssr_required, cms_required, dynamic_routes: bool
#   stateful_ui_components: int — count of genuinely stateful reusable widgets
#   shared_motion_primitives: bool — reusable motion system across sections
#   isolated_effects: int       — decorative one-off effects (canvas bg, cursor)
#   needs_scene_graph, needs_camera_paths, needs_3d_materials,
#   needs_3d_interactivity: bool
#   current_profile: str|None   — existing project's profile, if any

MANIFEST_BOOLS = ("content_site", "ssr_required", "cms_required",
                  "dynamic_routes", "shared_motion_primitives",
                  "needs_scene_graph", "needs_camera_paths",
                  "needs_3d_materials", "needs_3d_interactivity")
MANIFEST_INTS = ("stateful_ui_components", "isolated_effects")


class ManifestError(Exception):
    pass


def validate_manifest(manifest: dict) -> dict:
    known = set(MANIFEST_BOOLS) | set(MANIFEST_INTS) | {"current_profile", "name"}
    unknown = set(manifest) - known
    if unknown:
        raise ManifestError(f"unknown manifest fields: {sorted(unknown)}")
    out = {}
    for key in MANIFEST_BOOLS:
        val = manifest.get(key, False)
        if not isinstance(val, bool):
            raise ManifestError(f"{key} must be bool, got {val!r}")
        out[key] = val
    for key in MANIFEST_INTS:
        val = manifest.get(key, 0)
        if not isinstance(val, int) or val < 0:
            raise ManifestError(f"{key} must be a non-negative int, got {val!r}")
        out[key] = val
    profile = manifest.get("current_profile")
    if profile is not None and profile not in PROFILES:
        raise ManifestError(f"current_profile {profile!r} not in {PROFILES}")
    out["current_profile"] = profile
    out["name"] = manifest.get("name")
    return out


def recommend(manifest: dict) -> dict:
    """-> {profile, reasons: [...], owner_gate: str|None, constraints: [...]}"""
    m = validate_manifest(manifest)
    reasons: list[str] = []
    constraints: list[str] = []

    real_3d = any((m["needs_scene_graph"], m["needs_camera_paths"],
                   m["needs_3d_materials"], m["needs_3d_interactivity"]))
    react_pressure = (m["stateful_ui_components"] >= 3
                      or m["shared_motion_primitives"])

    if real_3d:
        profile = "react-3d"
        needs = [k for k in ("needs_scene_graph", "needs_camera_paths",
                             "needs_3d_materials", "needs_3d_interactivity")
                 if m[k]]
        reasons.append(f"real 3D requirements: {', '.join(needs)}")
        constraints += ["3D canvas must be a lazy island with static fallback",
                        "GPU/bundle budgets frozen after baseline spike"]
    elif react_pressure:
        profile = "react-motion"
        if m["stateful_ui_components"] >= 3:
            reasons.append(
                f"{m['stateful_ui_components']} stateful reusable components "
                f"justify a component runtime")
        if m["shared_motion_primitives"]:
            reasons.append("shared motion primitives across sections")
        constraints.append("WebGL only as a lazy island, never eagerly")
    else:
        profile = "static-lite"
        reasons.append("no real 3D and no stateful-UI pressure — "
                       "cheapest sufficient runtime")
        if m["isolated_effects"]:
            reasons.append(
                f"{m['isolated_effects']} isolated decorative effect(s) stay "
                f"vanilla JS/Canvas islands — React is never adopted for a "
                f"lone effect (decision §2)")
        constraints.append("effects as self-contained JS islands, no framework")

    if m["ssr_required"] or m["cms_required"] or m["dynamic_routes"]:
        needs = [k for k in ("ssr_required", "cms_required", "dynamic_routes")
                 if m[k]]
        profile = "next-content"
        reasons.append(f"explicit content-platform needs: {', '.join(needs)} "
                       f"(next-content is never a default)")
        constraints.append("motion/3D layer chosen separately on top")

    owner_gate = None
    if m["current_profile"] and m["current_profile"] != profile:
        owner_gate = (f"changing an existing project from "
                      f"{m['current_profile']} to {profile} is an OWNER "
                      f"decision — the router only recommends")
    return {"profile": profile, "reasons": reasons,
            "constraints": constraints, "owner_gate": owner_gate}
