"""design-scout CLI (CONTRACT.md schema 1).

Commands: sources | probe | collect | selftest. Exit semantics per §2:
probe/selftest are evaluative (0/1/2), sources informational, collect
generating (0 on run completion, per-source statuses inside the envelope)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ._contract import (EXIT_FAIL, EXIT_PASS, EXIT_TOOL_ERROR, ContractError,
                        atomic_write_text, emit, envelope, resolve_out)
from .fetch import BudgetLog, Fetcher
from .registry import load_registry, source_key  # noqa: F401 (source_key: sources cmd)

from . import SCHEMA_VERSION as SCHEMA_VERSION   # re-export: tests read cli.SCHEMA_VERSION


def _cmd_sources(args) -> int:
    registry = load_registry(Path(args.registry) if args.registry else None)
    rows = [{
        "name": s["name"], "method": s["method"],
        "enabled": s.get("enabled", True),
        "license_policy": s["license_policy"],
        "key_env": s.get("key_env"),
        "key_present": bool(source_key(s)) if s.get("key_env") else None,
        "disabled_reason": s.get("disabled_reason"),
        "known_issue": s.get("known_issue"),
    } for s in registry["sources"]]
    env = envelope("sources", {"registry": args.registry or "builtin"},
                   data={"budgets": registry["budgets"], "sources": rows})
    emit(env, args.json)
    return EXIT_PASS


def _cmd_probe(args) -> int:
    from .collect import run_probe
    registry = load_registry(Path(args.registry) if args.registry else None)
    results, passed, log = run_probe(registry)
    floor = results.pop("_floor")
    verdict = {
        "result": "PASS" if passed else "FAIL",
        "reason": f"live={floor['live']} (floor {floor['floor_live_total']}), "
                  f"live_api={floor['live_api']} (floor {floor['floor_live_api']})",
    }
    env = envelope("probe", {"registry": args.registry or "builtin"},
                   data={"sources": results, "budget_log": log.as_dict()},
                   verdict=verdict)
    emit(env, args.json)
    return EXIT_PASS if passed else EXIT_FAIL


def _cmd_collect(args) -> int:
    from .collect import run_collect
    from .moodboard import render
    registry = load_registry(Path(args.registry) if args.registry else None)
    queries = [q for q in (args.query or []) if q.strip()]
    if args.queries_file:
        # utf-8-sig: Notepad/PowerShell BOM must not leak into API queries
        # (vault rule "stdin хуков читать utf-8-sig", review F6)
        text = Path(args.queries_file).read_text(encoding="utf-8-sig")
        queries += [ln.strip() for ln in text.splitlines()
                    if ln.strip() and not ln.strip().startswith("#")]
    if not queries:
        raise ContractError("at least one --query (or --queries-file) is required")
    out_dir = resolve_out(args.out)
    log = BudgetLog()
    fetch = Fetcher(registry["budgets"], log)
    manifest = run_collect(registry, queries, out_dir, fetch, log)
    from ._contract import tool_versions
    manifest["tool"] = tool_versions()
    manifest_path = out_dir / "manifest.json"
    atomic_write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                      manifest_path)
    board_path = out_dir / "moodboard.html"
    atomic_write_text(render(manifest), board_path)
    outputs = [str(manifest_path), str(board_path)]
    outputs += [str(out_dir / r["local_thumb"]) for r in manifest["records"]
                if r.get("local_thumb")]
    env = envelope("collect",
                   {"queries": queries, "out": str(out_dir),
                    "registry": args.registry or "builtin"},
                   data={"sources": manifest["sources"],
                         "records": len(manifest["records"]),
                         "dedup_dropped": manifest["dedup_dropped"],
                         "budget_log": manifest["run"]["budget_log"]},
                   outputs=outputs)
    emit(env, args.json)
    return EXIT_PASS


def _cmd_selftest(args) -> int:
    """Offline contract check with a canned fetcher — no network (§2)."""
    from .selftest import run_selftest
    failures, data = run_selftest()
    verdict = {"result": "PASS" if not failures else "FAIL",
               "reason": "; ".join(failures) or f"{data['checks']} checks green"}
    env = envelope("selftest", {}, data=data, verdict=verdict)
    emit(env, args.json)
    return EXIT_PASS if not failures else EXIT_FAIL


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="design-scout",
        description="Deterministic design-reference collector (contract schema 1)")
    from . import __version__
    parser.add_argument("--version", action="version",
                        version=f"design-scout {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func in (("sources", _cmd_sources), ("probe", _cmd_probe),
                       ("collect", _cmd_collect), ("selftest", _cmd_selftest)):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument("--registry", help="alternate sources.json (tests)")
        p.set_defaults(func=func)
        if name == "collect":
            p.add_argument("--query", action="append",
                           help="search query (repeatable, RU/EN)")
            p.add_argument("--queries-file",
                           help="file with one query per line (# comments)")
            p.add_argument("--out", help="output directory (required, §3)")
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
        # argparse errors must keep the JSON contract (imgqa F2 lesson)
        if exc.code not in (0, None):
            emit(envelope("argparse", {"argv": argv or sys.argv[1:]},
                          error="invalid arguments (see stderr)"), as_json=True)
            return EXIT_TOOL_ERROR
        return 0
    try:
        return args.func(args)
    except ContractError as exc:
        emit(envelope(args.command, {}, error=str(exc)),
             as_json=getattr(args, "json", False))
        return EXIT_TOOL_ERROR
    except Exception as exc:  # noqa: BLE001 — §6: raw exceptions never escape
        emit(envelope(args.command, {},
                      error=f"unexpected {type(exc).__name__}: {exc}"),
             as_json=getattr(args, "json", False))
        return EXIT_TOOL_ERROR


if __name__ == "__main__":
    sys.exit(main())
