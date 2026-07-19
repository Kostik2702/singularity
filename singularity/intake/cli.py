"""component-intake CLI (CONTRACT.md): run | selftest.

v1 verdict ceiling is ADAPT: runtime phases (usage fixture, Playwright,
bundle delta) are v2 and reported as typed unavailable — a verdict can not
reach PASS on unexecuted evidence. REJECT semantics are fully live."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

from singularity.scout.fetch import BudgetLog, Fetcher

from . import inspect as phases

SCHEMA = "component-intake/1"
EXIT_PASS, EXIT_NOT_PASS, EXIT_TOOL_ERROR = 0, 1, 2

ENGINE_ROOT = Path(__file__).resolve().parents[2]


class IntakeError(Exception):
    pass


def envelope(command: str, inputs: dict, data=None, verdict=None,
             outputs=None, error=None) -> dict:
    from . import __version__
    env = {"schema_version": SCHEMA, "command": command,
           "status": "error" if error else "ok",
           "tool": {"component-intake": __version__}, "inputs": inputs}
    for key, val in (("data", data), ("verdict", verdict),
                     ("outputs", outputs), ("error", error)):
        if val is not None:
            env[key] = val
    return env


def resolve_workspace(workspace: str | None, name: str) -> Path:
    if workspace:
        ws = Path(workspace).resolve()
    else:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        ws = Path(tempfile.gettempdir()) / "component-intake" / f"{digest}-{name[:40]}"
    protected = [ENGINE_ROOT] + [Path(r).resolve() for r in filter(
        None, os.environ.get("INTAKE_PROTECTED_ROOTS", "").split(os.pathsep))]
    for root in protected:
        if ws == root or root in ws.parents or ws in root.parents:
            raise IntakeError(
                f"workspace {ws} is inside/above protected root {root} (§3)")
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def _safe_name(path: str) -> str:
    """Flatten to the basename — a registry path may not traverse out (§3)."""
    name = Path(path.replace("\\", "/")).name
    return name or "unnamed"


def load_payload(args, fetch) -> tuple[dict, dict]:
    """-> (payload {files, dependencies, name}, provenance dict)"""
    record = None
    if args.candidates and args.id:
        doc = json.loads(Path(args.candidates).read_text(encoding="utf-8-sig"))
        for cand in doc.get("candidates", []):
            if cand.get("candidate_id") == args.id:
                record = cand
                break
        if record is None:
            raise IntakeError(f"candidate {args.id} not found in {args.candidates}")
    source = args.source or (record or {}).get("source_url")
    if args.source_file:
        raw = Path(args.source_file).read_text(encoding="utf-8-sig")
        origin = str(Path(args.source_file).resolve())
    elif source:
        res = fetch(source)
        if not res.ok:
            raise IntakeError(f"fetch failed: {source} ({res.error_class})")
        raw = res.body.decode("utf-8", errors="replace")
        origin = source
    else:
        raise IntakeError("need --source, --source-file, or --candidates/--id")
    try:
        item = json.loads(raw)
    except ValueError as exc:
        raise IntakeError(f"source is not registry-item JSON: {exc}") from exc
    files = [{"path": _safe_name(f.get("path") or f.get("target") or "unnamed"),
              "content": f.get("content") or ""}
             for f in item.get("files", [])]
    if not files:
        raise IntakeError("registry item has no files[] — nothing to inspect")
    payload = {
        "name": item.get("name") or (record or {}).get("name") or "unnamed",
        "files": files,
        "dependencies": sorted(set((item.get("dependencies") or [])
                                   + list((record or {}).get("dependencies") or []))),
    }
    provenance = {
        "origin": origin,
        "retrieved_at": phases.now_iso(),
        "candidate_id": (record or {}).get("candidate_id"),
        "record_license": (record or {}).get("license"),
        "record_provider": (record or {}).get("provider"),
    }
    return payload, provenance


def run_intake(payload: dict, provenance: dict, workspace: Path,
               runtime: bool = False,
               playwright_project: str | None = None) -> dict:
    files = payload["files"]
    src_dir = workspace / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        (src_dir / f["path"]).write_text(f["content"], encoding="utf-8")

    checks: dict[str, dict] = {}
    reject: list[str] = []
    requirements: list[str] = []
    risks: list[str] = []

    checks["snapshot"] = {"status": "ok", "files": phases.snapshot_files(files)}

    lic = phases.classify_license(provenance.get("record_license"), files)
    checks["license"] = {"status": "ok" if lic["license"] != "unknown" else "fail",
                         **lic}
    if lic["license"] == "unknown":
        reject.append("license unknown — reference-only (contract §2.4)")

    script_findings = phases.scan_scripts(payload)
    checks["scripts"] = {"status": "fail" if script_findings else "ok",
                         "findings": script_findings}
    reject += [f"lifecycle scripts denied: {f}" for f in script_findings]

    static = phases.scan_static(files)
    checks["static-scan"] = {"status": "fail" if static["reject"] else "ok",
                             **static}
    reject += static["reject"]
    risks += static["risks"]

    deps = phases.review_deps(payload["dependencies"])
    checks["deps"] = {"status": "ok", **deps,
                      "declared": payload["dependencies"]}
    requirements += deps["requirements"]
    risks += deps["risks"]

    build = phases.check_build(files, workspace)
    checks["build"] = build
    if build["status"] == "fail":
        reject += [f"build/transform failed: {f}" for f in build.get("failures", [])]
    elif build["status"] == "unavailable":
        requirements.append(f"build proof unavailable: {build['why']}")

    reduced = phases.check_reduced_motion(files)
    checks["reduced-motion"] = reduced
    if reduced["status"] == "adapt":
        requirements.append(reduced["requirement"])

    if runtime:
        from .runtime import run_runtime
        rt = run_runtime(files, workspace, playwright_project)
    else:
        rt = {"status": "unavailable",
              "why": "runtime phase not requested (--runtime)"}
    checks["runtime"] = rt
    if rt["status"] == "fail":
        reject += [f"runtime: {f}" for f in rt.get("failures", [])]
    elif rt["status"] == "unavailable":
        requirements.append(f"runtime evidence unavailable: {rt['why']} — "
                            f"verdict capped at ADAPT")
    # rt ok lifts the v1 cap: PASS is reachable on full runtime evidence

    if reject:
        result = "REJECT"
    elif requirements or risks:
        result = "ADAPT"
    else:
        result = "PASS"  # unreachable in v1 by design (runtime cap)
    return {
        "checks": checks,
        "reject_reasons": reject,
        "requirements": requirements,
        "risks": risks,
        "provenance": provenance,
        "verdict": {"result": result,
                    "reason": "; ".join(reject or requirements or ["clean"])[:500]},
    }


def _cmd_run(args) -> int:
    log = BudgetLog()
    fetch = Fetcher({"per_fetch_timeout_s": 20, "retry_max": 1,
                     "retryable_classes": ["timeout", "http-5xx"],
                     "wall_budget_s": 120}, log)
    payload, provenance = load_payload(args, fetch)
    workspace = resolve_workspace(args.workspace, payload["name"])
    result = run_intake(payload, provenance, workspace,
                        runtime=args.runtime,
                        playwright_project=args.playwright_project)
    report_path = workspace / "intake-report.json"
    tmp = report_path.with_name(report_path.name + ".tmp")
    tmp.write_text(json.dumps({"schema_version": SCHEMA, **result},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(report_path)
    env = envelope("run",
                   {"id": args.id, "source": args.source,
                    "source_file": args.source_file,
                    "workspace": str(workspace)},
                   data={k: result[k] for k in
                         ("checks", "reject_reasons", "requirements", "risks",
                          "provenance")},
                   verdict=result["verdict"],
                   outputs=[str(report_path)])
    _emit(env, args.json)
    return EXIT_PASS if result["verdict"]["result"] == "PASS" else EXIT_NOT_PASS


def _emit(env: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
        return
    line = f"[component-intake {env['command']}] {env['status']}"
    if env.get("verdict"):
        line += f" | {env['verdict']['result']} ({env['verdict']['reason'][:160]})"
    if env.get("error"):
        line += f" | error: {env['error']}"
    print(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="component-intake",
        description="Quarantine gate for external candidates (WSF-E03, "
                    "inspect-first v1)")
    from . import __version__
    parser.add_argument("--version", action="version",
                        version=f"component-intake {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run")
    p.add_argument("--source", help="registry item JSON URL")
    p.add_argument("--source-file", help="registry item JSON on disk (offline)")
    p.add_argument("--candidates", help="E02 candidates.json (provenance chain)")
    p.add_argument("--id", help="candidate_id inside --candidates")
    p.add_argument("--workspace")
    p.add_argument("--runtime", action="store_true",
                   help="v2: bundle + network-quarantined live run (needs "
                        "INTAKE_ESBUILD, INTAKE_DEPS_ROOT, "
                        "INTAKE_PLAYWRIGHT_PROJECT)")
    p.add_argument("--playwright-project",
                   help="project root whose node_modules has playwright")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=_cmd_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            _emit(envelope("argparse", {}, error="invalid arguments"), True)
            return EXIT_TOOL_ERROR
        return 0
    try:
        return args.func(args)
    except IntakeError as exc:
        _emit(envelope(args.command, {}, error=str(exc)),
              getattr(args, "json", False))
        return EXIT_TOOL_ERROR
    except Exception as exc:  # noqa: BLE001
        _emit(envelope(args.command, {},
                       error=f"unexpected {type(exc).__name__}: {exc}"),
              getattr(args, "json", False))
        return EXIT_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
