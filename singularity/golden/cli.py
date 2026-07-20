"""golden CLI (golden-registry/1): add | search | install | promote | list.

Store layout (GOLDEN_REGISTRY_DIR, default <engine>/golden-registry):
  items/<name>.json   — item record (recipe, provenance, capabilities,
                        status candidate|golden, promotion evidence)
  files/<name>/...    — the adapted source files (offline, no provider dep)

Rules encoded from the decision/backlog:
- add REFUSES an item without a known license, a complete recipe (unresolved
  TODOs fail), a reduced/static fallback statement and acceptance evidence —
  the curator check is the door, not a later cleanup;
- every item is born status=candidate; promote to golden ONLY with
  --reuse-evidence (second successful application) or --owner-decision;
- upstream updates never flow in automatically: a new upstream revision is a
  NEW intake (E03), never an in-place file edit;
- install copies files offline into --dest (explicit, no magic paths).

Exit: informational commands 0/2; search returns 0 even when empty (the
result count is data, not a verdict)."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "golden-registry/1"
EXIT_OK, EXIT_TOOL_ERROR = 0, 2
_NAME_RE = re.compile(r"^[a-z0-9-]+$")


class GoldenError(Exception):
    pass


def registry_dir() -> Path:
    root = os.environ.get("GOLDEN_REGISTRY_DIR")
    if root:
        return Path(root)
    return Path(__file__).resolve().parents[2] / "golden-registry"


def _items_dir() -> Path:
    return registry_dir() / "items"


def _files_dir(name: str) -> Path:
    return registry_dir() / "files" / name


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def envelope(command: str, inputs: dict, data=None, outputs=None,
             error=None) -> dict:
    from . import __version__
    env = {"schema_version": SCHEMA, "command": command,
           "status": "error" if error else "ok",
           "tool": {"golden": __version__}, "inputs": inputs}
    for key, val in (("data", data), ("outputs", outputs), ("error", error)):
        if val is not None:
            env[key] = val
    return env


def _emit(env: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(env, ensure_ascii=False))
        return
    line = f"[golden {env['command']}] {env['status']}"
    if env.get("error"):
        line += f" | error: {env['error']}"
    print(line)
    for key, val in (env.get("data") or {}).items():
        print(f"  {key}: {json.dumps(val, ensure_ascii=False)[:400]}")


def _load_item(name: str) -> dict:
    path = _items_dir() / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise GoldenError(f"item {name} unreadable: {exc}") from exc


def _write_item(item: dict) -> Path:
    dest = _items_dir() / f"{item['name']}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(json.dumps(item, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    tmp.replace(dest)
    return dest


def _cmd_add(args) -> int:
    from singularity.intake.recipe import load as load_recipe
    from singularity.intake.recipe import validate as validate_recipe
    if not _NAME_RE.match(args.name):
        raise GoldenError(f"name {args.name!r} must match [a-z0-9-]+")
    if (_items_dir() / f"{args.name}.json").exists() and not args.force:
        raise GoldenError(f"item {args.name} already exists (supersede with "
                          f"a NEW name + deprecation, or --force)")
    recipe = load_recipe(Path(args.recipe))
    problems = validate_recipe(recipe)
    if problems:
        raise GoldenError("recipe incomplete — the door check refuses: "
                          + "; ".join(problems[:5]))
    lic = recipe.get("license") or {}
    lic_name = lic.get("license") if isinstance(lic, dict) else str(lic)
    if not lic_name or str(lic_name).lower() == "unknown":
        raise GoldenError("item without a known license cannot enter the registry")
    src_dir = Path(args.files)
    files = [p for p in src_dir.rglob("*") if p.is_file()]
    if not files:
        raise GoldenError(f"--files {src_dir} has no files")
    dest_root = _files_dir(args.name)
    dest_root.mkdir(parents=True, exist_ok=True)
    stored = []
    for p in files:
        rel = p.relative_to(src_dir)
        target = dest_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, target)
        stored.append(str(rel))
    item = {
        "schema_version": SCHEMA,
        "name": args.name,
        "status": "candidate",
        "capabilities": sorted({c.strip() for c in
                                (args.capability or []) if c.strip()}),
        "style_tags": sorted({t.strip() for t in (args.tag or []) if t.strip()}),
        "stack": args.stack or "unknown",
        "recipe": recipe,
        "files": stored,
        "registered_at": now_iso(),
        "applications": [{"project": args.project or "unknown",
                          "at": now_iso(), "evidence": args.evidence}],
        "promotion": None,
        "deprecation": None,
    }
    path = _write_item(item)
    _emit(envelope("add", {"name": args.name},
                   data={"status": "candidate", "files": len(stored)},
                   outputs=[str(path)]), args.json)
    return EXIT_OK


def _cmd_promote(args) -> int:
    item = _load_item(args.name)
    if item["status"] == "golden":
        raise GoldenError(f"{args.name} is already golden")
    if args.reuse_evidence:
        item["applications"].append({"project": args.project or "unknown",
                                     "at": now_iso(),
                                     "evidence": args.reuse_evidence})
        if len(item["applications"]) < 2:
            raise GoldenError("promotion needs a SECOND successful application")
        item["promotion"] = {"by": "reuse", "at": now_iso(),
                             "evidence": args.reuse_evidence}
    elif args.owner_decision:
        item["promotion"] = {"by": "owner-decision", "at": now_iso(),
                             "decision": args.owner_decision}
    else:
        raise GoldenError("promotion gate: provide --reuse-evidence (second "
                          "application) or --owner-decision (explicit)")
    item["status"] = "golden"
    path = _write_item(item)
    _emit(envelope("promote", {"name": args.name},
                   data={"status": "golden", "promotion": item["promotion"]},
                   outputs=[str(path)]), args.json)
    return EXIT_OK


def _iter_items() -> list[dict]:
    items_dir = _items_dir()
    if not items_dir.is_dir():
        return []
    out = []
    for p in sorted(items_dir.glob("*.json")):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError):
            continue
    return out


def _cmd_search(args) -> int:
    wanted_caps = {c.strip() for c in (args.capability or []) if c.strip()}
    text = (args.text or "").lower()
    rows = []
    for item in _iter_items():
        if args.status and item.get("status") != args.status:
            continue
        if wanted_caps and not wanted_caps <= set(item.get("capabilities", [])):
            continue
        blob = json.dumps([item.get("name"), item.get("style_tags"),
                           item.get("recipe", {}).get("preserved_intent")],
                          ensure_ascii=False).lower()
        if text and text not in blob:
            continue
        rows.append({"name": item["name"], "status": item["status"],
                     "capabilities": item.get("capabilities"),
                     "stack": item.get("stack"),
                     "applications": len(item.get("applications", []))})
    _emit(envelope("search", {"capability": sorted(wanted_caps),
                              "text": args.text, "status": args.status},
                   data={"count": len(rows), "items": rows}), args.json)
    return EXIT_OK


def _cmd_install(args) -> int:
    item = _load_item(args.name)
    src_root = _files_dir(args.name)
    dest = Path(args.dest).resolve()
    engine = Path(__file__).resolve().parents[2]
    if dest == engine or engine in dest.parents:
        raise GoldenError("--dest inside the engine repo is forbidden")
    copied = []
    for rel in item.get("files", []):
        src = src_root / rel
        if not src.is_file():
            raise GoldenError(f"registry corrupted: missing file {rel}")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(str(target))
    _emit(envelope("install", {"name": args.name, "dest": str(dest)},
                   data={"status": item["status"], "files": len(copied),
                         "license": item.get("recipe", {}).get("license"),
                         "note": "offline copy from the local registry — no "
                                 "provider involved"},
                   outputs=copied), args.json)
    return EXIT_OK


def _cmd_list(args) -> int:
    rows = [{"name": i["name"], "status": i["status"],
             "capabilities": i.get("capabilities")} for i in _iter_items()]
    _emit(envelope("list", {}, data={"count": len(rows), "items": rows}),
          args.json)
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="golden", description="Local golden pattern registry (WSF-E07)")
    from . import __version__
    parser.add_argument("--version", action="version",
                        version=f"golden {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add")
    a.add_argument("--name", required=True)
    a.add_argument("--recipe", required=True, help="validated recipe.json")
    a.add_argument("--files", required=True, help="adapted source dir")
    a.add_argument("--capability", action="append")
    a.add_argument("--tag", action="append")
    a.add_argument("--stack")
    a.add_argument("--project")
    a.add_argument("--evidence", help="acceptance evidence path/url")
    a.add_argument("--force", action="store_true")
    a.set_defaults(func=_cmd_add)

    p = sub.add_parser("promote")
    p.add_argument("--name", required=True)
    p.add_argument("--reuse-evidence")
    p.add_argument("--project")
    p.add_argument("--owner-decision")
    p.set_defaults(func=_cmd_promote)

    s = sub.add_parser("search")
    s.add_argument("--capability", action="append")
    s.add_argument("--text")
    s.add_argument("--status", choices=["candidate", "golden"])
    s.set_defaults(func=_cmd_search)

    i = sub.add_parser("install")
    i.add_argument("--name", required=True)
    i.add_argument("--dest", required=True)
    i.set_defaults(func=_cmd_install)

    ls = sub.add_parser("list")
    ls.set_defaults(func=_cmd_list)

    for cmd_parser in (a, p, s, i, ls):
        cmd_parser.add_argument("--json", action="store_true")
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
    except GoldenError as exc:
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
