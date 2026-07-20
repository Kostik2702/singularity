"""Candidate scoring (WSF-E02, decision §5): base score out of 100 with a
per-component evidence trail. Deterministic — every number is explainable;
the score only forms the SHORTLIST, the final visual choice stays a human
gate. Components (decision weights):

  brief/design fit 25 | license/provenance 20 | stack compatibility 15 |
  measured performance potential 15 | dependency/maintenance cost 10 |
  accessibility/reduced-motion 10 | source freshness 5

Unknowable-in-v1 components score a documented neutral default, never a
silent max: a candidate cannot win on missing data."""
from __future__ import annotations

HEAVY_DEPS = {"three", "@react-three/fiber", "@react-three/drei", "gsap"}

PROFILE_FRAMEWORKS = {
    "static-lite": {"static"},
    "react-motion": {"react", "static"},
    "react-3d": {"react", "three", "r3f", "static"},
    "next-content": {"next", "react", "static"},
}


def score_candidate(cand: dict, *, brief_fit: float = 0.0,
                    project_profile: str | None = None) -> dict:
    """-> {"total": int, "components": {name: {"points", "max", "why"}}}
    brief_fit in [0,1] comes from the caller (match relevance or the skill's
    judgment); it is evidence-logged, not invented here."""
    parts: dict[str, dict] = {}

    def add(name: str, points: float, maximum: int, why: str):
        parts[name] = {"points": round(points, 1), "max": maximum, "why": why}

    add("brief_fit", max(0.0, min(1.0, brief_fit)) * 25, 25,
        f"caller-supplied fit {brief_fit:.2f} × 25")

    lic = (cand.get("license") or "unknown").lower()
    if lic == "unknown":
        add("license", 0, 20, "license unknown — reference-only")
    elif cand.get("price_tier") == "paid":
        add("license", 5, 20, "known license but paid (report-only)")
    elif cand.get("importable"):
        add("license", 20, 20, f"known free license: {cand.get('license')}")
    else:
        add("license", 8, 20,
            f"license known but blocked: {cand.get('import_block_reason')}")

    fw = cand.get("framework", "unknown")
    if project_profile is None:
        add("stack", 8, 15, "no project profile given — neutral default")
    elif fw in PROFILE_FRAMEWORKS.get(project_profile, set()):
        add("stack", 15, 15, f"{fw} fits profile {project_profile}")
    elif fw == "unknown":
        add("stack", 5, 15, "framework unknown")
    else:
        add("stack", 2, 15,
            f"{fw} needs a port to profile {project_profile} (cost of port)")

    deps = cand.get("dependencies") or []
    heavy = [d for d in deps if d in HEAVY_DEPS]
    if not deps:
        add("deps", 10, 10, "zero dependencies")
    elif len(deps) <= 2 and not heavy:
        add("deps", 7, 10, f"{len(deps)} light deps: {deps}")
    elif len(deps) <= 4:
        add("deps", 4, 10, f"{len(deps)} deps, heavy: {heavy or 'none'}")
    else:
        add("deps", 1, 10, f"{len(deps)} deps — maintenance cost")

    kind = cand.get("kind")
    if kind in ("motion-recipe", "example") and not heavy:
        add("perf", 11, 15, "recipe/example without heavy runtime")
    elif heavy:
        add("perf", 5, 15, f"heavy runtime deps {heavy} — lazy-island required")
    else:
        add("perf", 8, 15, "unmeasured — neutral until E06 baseline")

    add("a11y", 5, 10, "reduced-motion behavior unknown until intake (E03)")
    add("freshness", 2, 5,
        "source revision not tracked in v1 — neutral default")

    total = round(sum(p["points"] for p in parts.values()))
    return {"total": total, "components": parts}


def hard_fail_reasons(cand: dict) -> list[str]:
    """Shortlist hard-fails (decision §4) — orthogonal to the score."""
    reasons = []
    if not cand.get("importable"):
        reasons.append(cand.get("import_block_reason") or "not importable")
    if cand.get("stale"):
        reasons.append("stale cached record — re-verify before shortlist")
    return reasons
