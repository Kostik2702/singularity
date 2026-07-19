"""factory CLI (factory-status/1): status — where is this project on the
E09 graph, what runs next, which owner gate blocks.

State is COMPUTED, never stored: each stage's typed output file is its
resume-point (E09 §typed-io); a stage is done when its artifact exists and
parses. The evidence dir is the project's out-of-tree evidence root the
tools already write to (moodboard, candidates, intake, recipes, qa)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCHEMA = "factory-status/1"
EXIT_OK, EXIT_TOOL_ERROR = 0, 2

# stage id -> (artifact glob relative to evidence root, human next-action,
#              owner gate that consumes the artifact or None)
STAGES = [
    ("S1-moodboard", "**/moodboard.html",
     "design-scout collect --query ... --out <evidence>/scout-<date>",
     "owner: art-direction acceptance"),
    ("S2-design-brief", "**/*design-brief*.md",
     "design-analysis skill over the mockup",
     "owner: brief acceptance"),
    ("S3-profile", ".factory/manifest.json",
     "fill .factory/manifest.json, run stackrouter (profile CHANGE of an "
     "existing project is an owner gate reported by the router itself)",
     None),
    ("S4-shortlist", "**/review-board.html",
     "pattern-scout search --board --profile <profile> --out <evidence>",
     "owner: shortlist"),
    ("S5-intake", "**/intake-report.json",
     "component-intake run --candidates ... --id ... --runtime",
     None),
    ("S6-recipe", "**/recipe*.json",
     "component-intake recipe --report ... then pattern-adapter, then --validate",
     None),
    ("S8-qa", "**/motion-qa-report.json",
     "implement per recipe (motion-3d-implementer), then motion-qa + determinism + a11y + fidelity",
     None),
    ("S9-baseline", "tests/visual/*-snapshots|__screenshots__",
     "owner runs test:update-baseline after visual acceptance",
     "owner: visual baseline (owner-only)"),
]


def envelope(command: str, inputs: dict, data=None, error=None) -> dict:
    from . import __version__
    env = {"schema_version": SCHEMA, "command": command,
           "status": "error" if error else "ok",
           "tool": {"factory": __version__}, "inputs": inputs}
    if data is not None:
        env["data"] = data
    if error is not None:
        env["error"] = error
    return env


def _exists(root: Path, pattern: str) -> str | None:
    if "|" in pattern:
        return next((p for pat in pattern.split("|")
                     for p in (_exists(root, pat),) if p), None)
    try:
        hit = next(root.glob(pattern), None)
    except (OSError, ValueError):
        hit = None
    return str(hit) if hit else None


def compute_status(project: Path, evidence: Path) -> dict:
    stages = []
    next_action = None
    pending_gate = None
    for stage_id, pattern, action, gate in STAGES:
        root = project if stage_id in ("S3-profile", "S9-baseline") else evidence
        artifact = _exists(root, pattern)
        done = artifact is not None
        stages.append({"stage": stage_id, "done": done, "artifact": artifact,
                       "gate": gate})
        if not done and next_action is None:
            next_action = f"{stage_id}: {action}"
            pending_gate = gate
    golden = []
    try:
        from singularity.golden.cli import _iter_items
        golden = [{"name": i["name"], "status": i["status"]}
                  for i in _iter_items()]
    except Exception:  # noqa: BLE001 — registry absence is not a status error
        pass
    return {
        "project": str(project),
        "evidence": str(evidence),
        "stages": stages,
        "next_action": next_action or "pipeline complete for this slice — "
                                      "next slice or golden promotion",
        "pending_gate": pending_gate,
        "golden_registry": golden,
    }


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(
        prog="factory", description="E09 pipeline state machine (WSF-E09)")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("status")
    s.add_argument("--project", required=True)
    s.add_argument("--evidence", required=True,
                   help="out-of-tree evidence root the pipeline tools wrote to")
    s.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            print(json.dumps(envelope("argparse", {}, error="invalid arguments")))
            return EXIT_TOOL_ERROR
        return 0
    try:
        project = Path(args.project).resolve()
        evidence = Path(args.evidence).resolve()
        if not project.is_dir():
            raise FileNotFoundError(f"project not found: {project}")
        data = compute_status(project, evidence)
        env = envelope("status", {"project": str(project)}, data=data)
        if args.json:
            print(json.dumps(env, ensure_ascii=False))
        else:
            print(f"[factory status] {project.name}")
            for st in data["stages"]:
                mark = "x" if st["done"] else " "
                print(f"  [{mark}] {st['stage']}"
                      + (f"  (gate: {st['gate']})" if st["gate"] and st["done"] else ""))
            print(f"  next: {data['next_action']}")
            if data["pending_gate"]:
                print(f"  gate: {data['pending_gate']}")
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001
        print(json.dumps(envelope("status", {},
                                  error=f"{type(exc).__name__}: {exc}")))
        return EXIT_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
