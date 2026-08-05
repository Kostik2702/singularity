"""imgqa CLI — deterministic image-QA toolkit (CONTRACT.md schema 1).

Exit codes (contract §2): evaluative commands 0=PASS/1=FAIL/2=tool error;
informational/transform/dispatch commands 0=success/2=tool error. Exit 2 must
never be read as "no defects".
"""
from __future__ import annotations

import argparse
import sys

from ._contract import (EXIT_FAIL, EXIT_PASS, EXIT_TOOL_ERROR, ContractError,
                        emit, envelope)

from . import SCHEMA_VERSION      # re-exported: callers and tests use cli.SCHEMA_VERSION

EVALUATIVE = {"probe", "compare", "contrast", "detect-fringe"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="imgqa",
        description="Deterministic image-QA toolkit (Singularity WEBSITE-FACTORY). "
                    "Contract: singularity/imgqa/CONTRACT.md (schema 1).")
    from . import __version__
    parser.add_argument("--version", action="version",
                        version=f"imgqa {__version__} (schema {SCHEMA_VERSION})")
    sub = parser.add_subparsers(dest="command")

    def add(name, help_line, configure):
        p = sub.add_parser(name, help=help_line)
        p.add_argument("--json", action="store_true", help="machine-readable envelope")
        configure(p)
        return p

    add("probe", "[evaluative] alpha/format/checkerboard probe",
        lambda p: p.add_argument("image"))
    add("edges", "[informational] lossless 200%% zoom crops of cutout edges",
        lambda p: (p.add_argument("image"), p.add_argument("--out", required=True),
                   p.add_argument("--max-regions", type=int, default=6)))
    add("backgrounds", "[informational] render subject on black/pink/white",
        lambda p: (p.add_argument("image"), p.add_argument("--out", required=True)))
    add("compare", "[evaluative] pixel diff + metrics between two images",
        lambda p: (p.add_argument("image_a"), p.add_argument("image_b"),
                   p.add_argument("--threshold", type=int, default=2),
                   p.add_argument("--bbox", default=None, help="X,Y,W,H region"),
                   p.add_argument("--max-mae", type=float, default=None),
                   p.add_argument("--max-diff-ratio", type=float, default=None)))
    add("contrast", "[evaluative] exact WCAG contrast ratio",
        lambda p: (p.add_argument("fg"), p.add_argument("bg"),
                   p.add_argument("--min", type=float, default=4.5)))
    add("sheet", "[transform] contact sheet for a video or directory",
        lambda p: (p.add_argument("target"), p.add_argument("--out", required=True),
                   p.add_argument("--cols", type=int, default=4),
                   p.add_argument("--rows", type=int, default=3),
                   p.add_argument("--width", type=int, default=320)))
    add("detect-fringe", "[evaluative] deterministic fringe detector -> bboxes",
        lambda p: p.add_argument("image"))
    add("defringe", "[transform] bbox-localised alpha de-fringe",
        lambda p: (p.add_argument("image"),
                   p.add_argument("--bbox", action="append", default=[],
                                  help="X,Y,W,H — repeatable; required"),
                   p.add_argument("--out", required=True),
                   p.add_argument("--feather", type=float, default=22.0),
                   p.add_argument("--low", type=int, default=95),
                   p.add_argument("--high", type=int, default=130)))
    add("cutout", "[dispatch] background removal via isolated ML venv (OD-1)",
        lambda p: (p.add_argument("image"), p.add_argument("--out", required=True)))
    add("selftest", "[informational] cross-shell sanity check",
        lambda p: None)
    return parser


def _dispatch(args):
    from . import external, fringe, ops
    table = {
        "probe": ops.run_probe,
        "edges": ops.run_edges,
        "backgrounds": ops.run_backgrounds,
        "compare": ops.run_compare,
        "contrast": ops.run_contrast,
        "sheet": external.run_sheet,
        "detect-fringe": fringe.run_detect_fringe,
        "defringe": fringe.run_defringe,
        "cutout": external.run_cutout,
        "selftest": external.run_selftest,
    }
    return table[args.command](args)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    wants_json = "--json" in raw_argv
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse already printed usage/--version output; on ERROR paths the
        # JSON contract still holds: emit a schema-compatible envelope (F2).
        if exc.code not in (0, None):
            command = next((a for a in raw_argv if not a.startswith("-")), "?")
            emit(envelope(command, inputs={"argv": raw_argv},
                          error="argument parsing failed — see usage on stderr"),
                 wants_json)
            return EXIT_TOOL_ERROR
        return 0
    if args.command is None:
        parser.print_help()
        return EXIT_TOOL_ERROR
    as_json = getattr(args, "json", False)
    try:
        inputs, data, verdict, outputs = _dispatch(args)
    except ContractError as exc:
        emit(envelope(args.command, inputs={}, error=str(exc)), as_json)
        return EXIT_TOOL_ERROR
    except Exception as exc:  # noqa: BLE001 — any crash is a tool error, never a verdict
        emit(envelope(args.command, inputs={},
                      error=f"{type(exc).__name__}: {exc}"), as_json)
        return EXIT_TOOL_ERROR
    emit(envelope(args.command, inputs=inputs, data=data, verdict=verdict,
                  outputs=outputs), as_json)
    if args.command in EVALUATIVE and verdict is not None:
        return EXIT_PASS if verdict["result"] == "PASS" else EXIT_FAIL
    return EXIT_PASS


if __name__ == "__main__":
    sys.exit(main())
