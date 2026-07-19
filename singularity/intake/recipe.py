"""Adaptation recipe contract (WSF-E04, decision §7): a component is never
copied as an opaque blob — the recipe carries the pattern's DNA and the
obligations. Schema adaptation-recipe/1; scaffold is generated FROM an
intake report (provenance + requirements feed straight in), completion is
the adapter skill's job, acceptance is the owner's."""
from __future__ import annotations

import json
from pathlib import Path

RECIPE_SCHEMA = "adaptation-recipe/1"

# field -> (required, description shown in the scaffold)
FIELDS = {
    "candidate_id":     (True,  "E02/E03 provenance chain id"),
    "upstream":         (True,  "origin url + per-file sha256 from the intake snapshot"),
    "license":          (True,  "license + obligations (attribution text if any)"),
    "preserved_intent": (True,  "which design/motion function is KEPT (the DNA)"),
    "replacements":     (True,  "design tokens / content / assets swapped in"),
    "timing":           (True,  "duration/easing/trigger model mapped to project motion tokens"),
    "breakpoints":      (True,  "desktop / mobile variants"),
    "reduced_motion":   (True,  "reduced variant behavior (from intake requirements)"),
    "static_fallback":  (True,  "no-JS / static meaning preservation"),
    "cleanup":          (True,  "lifecycle/unmount rules (listeners, rAF, contexts)"),
    "lazy_strategy":    (True,  "loading strategy (island, on-view, budget)"),
    "patch_set":        (True,  "local changes vs upstream, reviewable"),
    "acceptance":       (True,  "screenshots + perf evidence paths"),
    "target_files":     (True,  "candidate -> recipe -> files traceability"),
    "notes":            (False, "anything else the next reader needs"),
}

TODO = "TODO"


class RecipeError(Exception):
    pass


def scaffold_from_report(report: dict, candidate_id: str | None = None) -> dict:
    """intake-report.json -> recipe skeleton: provenance and requirements are
    facts and are filled in; judgment fields stay TODO for the adapter."""
    prov = report.get("provenance", {})
    checks = report.get("checks", {})
    recipe = {"schema_version": RECIPE_SCHEMA}
    for field, (_, desc) in FIELDS.items():
        recipe[field] = f"{TODO}: {desc}"
    recipe["candidate_id"] = (candidate_id or prov.get("candidate_id")
                              or f"{TODO}: candidate id")
    recipe["upstream"] = {
        "origin": prov.get("origin"),
        "retrieved_at": prov.get("retrieved_at"),
        "files": checks.get("snapshot", {}).get("files", []),
    }
    lic = checks.get("license", {})
    recipe["license"] = {"license": lic.get("license"),
                         "source": lic.get("source"),
                         "obligations": f"{TODO}: attribution/notice text if required"}
    reqs = report.get("requirements", [])
    reduced = [r for r in reqs if "reduced-motion" in r]
    recipe["reduced_motion"] = (reduced[0] if reduced
                                else f"{TODO}: confirm none needed (intake saw none)")
    lazy = [r for r in reqs if "lazy" in r]
    if lazy:
        recipe["lazy_strategy"] = lazy[0]
    recipe["intake_requirements"] = reqs
    recipe["intake_verdict"] = report.get("verdict", {}).get("result")
    return recipe


def validate(recipe: dict) -> list[str]:
    """-> problems list (empty = valid). A recipe with TODOs is a DRAFT and
    fails validation — it cannot enter an implementation branch (§7)."""
    problems = []
    if recipe.get("schema_version") != RECIPE_SCHEMA:
        problems.append(f"schema_version != {RECIPE_SCHEMA}")
    for field, (required, _) in FIELDS.items():
        val = recipe.get(field)
        if val in (None, "", [], {}):
            if required:
                problems.append(f"missing required field: {field}")
            continue
        if isinstance(val, str) and val.startswith(TODO):
            problems.append(f"unresolved TODO: {field}")
        if isinstance(val, dict):
            for k, v in val.items():
                if isinstance(v, str) and v.startswith(TODO):
                    problems.append(f"unresolved TODO: {field}.{k}")
    upstream = recipe.get("upstream")
    if isinstance(upstream, dict):
        files = upstream.get("files") or []
        if not files or not all(f.get("sha256") for f in files):
            problems.append("upstream.files must carry sha256 provenance")
    verdict = recipe.get("intake_verdict")
    if verdict == "REJECT":
        problems.append("intake verdict REJECT — a rejected candidate cannot "
                        "have a recipe (re-intake after upstream fix instead)")
    return problems


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RecipeError(f"recipe unreadable: {path}: {exc}") from exc
