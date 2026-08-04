"""pattern-scout CLI (CONTRACT.md §8): providers | probe | search | selftest."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters import ADAPTERS, run_search
from .candidate import CAPABILITIES
from .registry import RegistryError, load_registry

from singularity.scout.fetch import BudgetLog, Fetcher

ENGINE_ROOT = Path(__file__).resolve().parents[2]


def resolve_out(out: str) -> Path:
    """scout/imgqa §3 convention: --out never inside the engine repo or any
    PSCOUT_PROTECTED_ROOTS root (set by calling skills to the target project)."""
    out_dir = Path(out).resolve()
    protected = [ENGINE_ROOT] + [Path(r).resolve() for r in filter(
        None, os.environ.get("PSCOUT_PROTECTED_ROOTS", "").split(os.pathsep))]
    for root in protected:
        if out_dir == root or root in out_dir.parents or out_dir in root.parents:
            raise RegistryError(
                f"--out {out_dir} is inside/above protected root {root}")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


SCHEMA_VERSION = 1
EXIT_PASS, EXIT_FAIL, EXIT_TOOL_ERROR = 0, 1, 2


def envelope(command: str, inputs: dict, data=None, verdict=None,
             outputs=None, error=None) -> dict:
    from . import __version__
    env = {"schema_version": SCHEMA_VERSION, "command": command,
           "status": "error" if error else "ok",
           "tool": {"pattern-scout": __version__}, "inputs": inputs}
    for key, val in (("data", data), ("verdict", verdict),
                     ("outputs", outputs), ("error", error)):
        if val is not None:
            env[key] = val
    return env


def emit(env: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
        return
    line = f"[pattern-scout {env['command']}] {env['status']}"
    if env.get("verdict"):
        line += f" | {env['verdict'].get('result')} ({env['verdict'].get('reason')})"
    if env.get("error"):
        line += f" | error: {env['error']}"
    print(line)
    for key, val in (env.get("data") or {}).items():
        if key != "candidates":
            print(f"  {key}: {json.dumps(val, ensure_ascii=False)[:500]}")


def _cmd_providers(args) -> int:
    reg = load_registry(Path(args.registry) if args.registry else None)
    rows = [{"name": p["name"], "class": p["class"], "adapter": p["adapter"],
             "method": p["method"], "enabled": p.get("enabled", True),
             "license": p.get("license"), "disabled_reason": p.get("disabled_reason")}
            for p in reg["providers"]]
    emit(envelope("providers", {}, data={"budgets": reg["budgets"],
                                         "providers": rows}), args.json)
    return EXIT_PASS


def _cmd_probe(args) -> int:
    reg = load_registry(Path(args.registry) if args.registry else None)
    log = BudgetLog()
    fetch = Fetcher(reg["budgets"], log)
    results = {}
    live = 0
    for prov in reg["providers"]:
        name = prov["name"]
        if prov.get("enabled") is False:
            results[name] = {"status": "skipped:disabled"}
            continue
        adapter = ADAPTERS.get(prov["adapter"])
        if adapter is None:
            results[name] = {"status": "unavailable:adapter-error"}
            continue
        try:
            _, status = adapter(prov, ["design"], [],
                                {"fetch": fetch, "budgets": reg["budgets"]})
        except Exception as exc:  # noqa: BLE001
            results[name] = {"status": "unavailable:adapter-error",
                             "reason": str(exc)[:200]}
            continue
        results[name] = {"status": status}
        live += 1 if status == "live" else 0
    verdict = {"result": "PASS" if live >= 1 else "FAIL",
               "reason": f"{live} provider(s) live"}
    emit(envelope("probe", {}, data={"providers": results,
                                     "budget_log": log.as_dict()},
                  verdict=verdict), args.json)
    return EXIT_PASS if live >= 1 else EXIT_FAIL


def _cmd_search(args) -> int:
    reg = load_registry(Path(args.registry) if args.registry else None)
    queries = [q for q in (args.query or []) if q.strip()]
    if not queries:
        raise RegistryError("at least one --query is required")
    caps = [c.strip() for c in (args.capability or []) if c.strip()]
    unknown = set(caps) - CAPABILITIES
    if unknown:
        raise RegistryError(
            f"unknown capabilities {sorted(unknown)}; valid: {sorted(CAPABILITIES)}")
    log = BudgetLog()
    fetch = Fetcher(reg["budgets"], log)
    result = run_search(reg, queries, caps, fetch, log,
                        use_cache=not args.no_cache)
    outputs = []
    if args.out:
        out_dir = resolve_out(args.out)
        dest = out_dir / "candidates.json"
        tmp = dest.with_name(dest.name + f".tmp-{id(result)}")
        tmp.write_text(json.dumps(
            {"schema_version": SCHEMA_VERSION, "queries": queries,
             "capabilities": caps, **result}, ensure_ascii=False, indent=1),
            encoding="utf-8")
        tmp.replace(dest)
        outputs.append(str(dest))
        if args.board:
            from .adapters import _match_score
            from .board import render
            from .scoring import hard_fail_reasons, score_candidate
            scored = []
            for cand in result["candidates"]:
                fit = _match_score(
                    f"{cand.get('name', '')} {cand.get('description', '')}",
                    queries)
                cand["_hard_fails"] = hard_fail_reasons(cand)
                scored.append((cand, score_candidate(
                    cand, brief_fit=fit, project_profile=args.profile)))
            scored.sort(key=lambda t: -t[1]["total"])
            board_path = out_dir / "review-board.html"
            tmp_b = board_path.with_name(board_path.name + ".tmp")
            tmp_b.write_text(render(
                {"queries": queries, "capabilities": caps,
                 "providers": result["providers"]}, scored), encoding="utf-8")
            tmp_b.replace(board_path)
            outputs.append(str(board_path))
    data = {"providers": result["providers"],
            "candidates_total": len(result["candidates"]),
            "importable": sum(1 for c in result["candidates"] if c["importable"]),
            "budget_log": result["budget_log"],
            "candidates": result["candidates"]}
    emit(envelope("search", {"queries": queries, "capabilities": caps},
                  data=data, outputs=outputs), args.json)
    return EXIT_PASS


def _cmd_selftest(args) -> int:
    from .selftest import run_selftest
    failures, data = run_selftest()
    verdict = {"result": "PASS" if not failures else "FAIL",
               "reason": "; ".join(failures) or f"{data['checks']} checks green"}
    emit(envelope("selftest", {}, data=data, verdict=verdict), args.json)
    return EXIT_PASS if not failures else EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pattern-scout",
        description="Typed provider layer for installable patterns (WSF-E00)")
    from . import __version__
    parser.add_argument("--version", action="version",
                        version=f"pattern-scout {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("providers", _cmd_providers), ("probe", _cmd_probe),
                       ("search", _cmd_search), ("selftest", _cmd_selftest)):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument("--registry")
        p.set_defaults(func=func)
        if name == "search":
            p.add_argument("--query", action="append")
            p.add_argument("--capability", action="append",
                           help=f"one of {sorted(CAPABILITIES)}")
            p.add_argument("--out")
            p.add_argument("--no-cache", action="store_true")
            p.add_argument("--board", action="store_true",
                           help="also render review-board.html (scored)")
            p.add_argument("--profile", default=None,
                           help="project stack profile for stack-fit scoring")
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
            emit(envelope("argparse", {"argv": argv or sys.argv[1:]},
                          error="invalid arguments (see stderr)"), as_json=True)
            return EXIT_TOOL_ERROR
        return 0
    try:
        return args.func(args)
    except RegistryError as exc:
        emit(envelope(args.command, {}, error=str(exc)),
             as_json=getattr(args, "json", False))
        return EXIT_TOOL_ERROR
    except Exception as exc:  # noqa: BLE001
        emit(envelope(args.command, {},
                      error=f"unexpected {type(exc).__name__}: {exc}"),
             as_json=getattr(args, "json", False))
        return EXIT_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
