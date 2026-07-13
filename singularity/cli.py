"""mb — single CLI entry point (replaces the Genevra script sprawl).

mb search "<query>" [--top N] [--type T] [--tags a,b] [--links X] [--json] ...
mb daemon start|stop|status|reload|run
mb index rebuild|update [--files f1 f2]
mb watch
mb doctor [--fix] [--heavy] [--json]
mb note new <type> <title>
mb learn "<insight>" [--task ID]
mb hooks status|enable|disable [--json]
mb project list|add|remove|detect
mb digest plan [--project X] [--json]
mb digest mark <file> <section> | --all [--json]
mb nightly [--json]   # index update -> digest/curator/config snapshot -> trend -> markers sweep
mb init            # write ~/.singularity.json pointer + folder tree
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from . import __version__
from .config import load_config, write_pointer

MEMORY_FOLDERS = ["learnings/active", "learnings/archive", "decisions", "howto",
                  "incidents", "rules", "reference", "changelog", "templates",
                  "_moc", "daily"]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mb", description="Singularity memory brain CLI")
    p.add_argument("--vault", help="vault path (default: MB_HOME / ~/.singularity.json)")
    p.add_argument("--version", action="version", version=f"mb {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("search", help="semantic search over vault (+code)")
    s.add_argument("query")
    s.add_argument("--top", type=int, default=5)
    s.add_argument("--type", dest="filter_type")
    s.add_argument("--tags", dest="filter_tags")
    s.add_argument("--links", dest="filter_wikilinks",
                   help="only notes linked to [[X]]")
    s.add_argument("--project", dest="filter_project",
                   help="comma-separated ids/groups/general/personal (T04)")
    s.add_argument("--path-prefix")
    s.add_argument("--memory-only", action="store_true")
    s.add_argument("--code-only", action="store_true")
    s.add_argument("--full", action="store_true", help="full content, no excerpt cut")
    s.add_argument("--explain", action="store_true", help="include component scores")
    s.add_argument("--max-per-file", type=int, default=2,
                   help="cap results per file (0 = unlimited)")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("daemon", help="daemon control")
    d.add_argument("action", choices=["start", "stop", "status", "reload", "run"])

    i = sub.add_parser("index", help="index management")
    i.add_argument("action", choices=["rebuild", "update", "inspect"])
    i.add_argument("--files", nargs="*", help="update only these vault-relative files")
    i.add_argument("path", nargs="?", help="file to inspect (index inspect <path>)")

    sub.add_parser("watch", help="on-save incremental index watcher")

    doc = sub.add_parser("doctor", help="installation verification")
    doc.add_argument("--fix", action="store_true")
    doc.add_argument("--heavy", action="store_true")
    doc.add_argument("--json", action="store_true",
                     help="emit a single JSON report, no human output")

    cur = sub.add_parser("curator", help="vault health: validation, orphans, link suggestions")
    cur.add_argument("--json", action="store_true")

    st = sub.add_parser("selftest", help="end-to-end pipeline check (create/index/find/delete)")
    st.add_argument("--json", action="store_true")

    stat = sub.add_parser("status", help="unified health snapshot (index/daemon/watcher/scheduler)")
    stat.add_argument("--json", action="store_true")

    stt = sub.add_parser("stats", help="memory analytics (notes/chunks/types/coverage)")
    stt.add_argument("--json", action="store_true")

    bk = sub.add_parser("backup", help="backup/restore config + index pointer")
    bksub = bk.add_subparsers(dest="backup_action", required=True)
    bksub.add_parser("create", help="create a checksummed backup")
    bkl = bksub.add_parser("list", help="list backups")
    bkl.add_argument("--json", action="store_true")
    bkv = bksub.add_parser("verify", help="verify a backup's checksums")
    bkv.add_argument("id")
    bkr = bksub.add_parser("restore", help="restore config + index pointer from a backup")
    bkr.add_argument("id")

    cfgp = sub.add_parser("config", help="inspect/validate .mb/config.json")
    cfgsub = cfgp.add_subparsers(dest="config_action", required=True)
    cfgv = cfgsub.add_parser("validate", help="check schema (types/ranges/unknown keys)")
    cfgv.add_argument("--json", action="store_true")
    cfgg = cfgsub.add_parser("get", help="print a config value")
    cfgg.add_argument("key")
    cfgs = cfgsub.add_parser("set", help="set a config value (validated, atomic save)")
    cfgs.add_argument("key")
    cfgs.add_argument("value", help="JSON value; falls back to a string literal")

    pr = sub.add_parser("project", help="multi-project registry + cwd detection")
    prsub = pr.add_subparsers(dest="project_action", required=True)
    prl = prsub.add_parser("list", help="list registered projects")
    prl.add_argument("--json", action="store_true")
    prl.add_argument("--group", help="filter to one group (or a bare project id)")
    pra = prsub.add_parser("add", help="register a project (validated, atomic)")
    pra.add_argument("id")
    pra.add_argument("path")
    pra.add_argument("--group")
    pra.add_argument("--about")
    prr = prsub.add_parser("remove", help="unregister a project")
    prr.add_argument("id")
    prd = prsub.add_parser("detect", help="resolve a path (default: cwd) to a registered project")
    prd.add_argument("path", nargs="?")
    prd.add_argument("--json", action="store_true")

    hk = sub.add_parser("hooks", help="capture/stop hooks control (desired/installed/effective)")
    hksub = hk.add_subparsers(dest="hooks_action", required=True)
    hkst = hksub.add_parser("status", help="show capture hooks desired/installed/effective state")
    hkst.add_argument("--json", action="store_true")
    hksub.add_parser("enable", help="enable capture hooks (auto_capture=true)")
    hksub.add_parser("disable", help="disable capture hooks (auto_capture=false)")

    dg = sub.add_parser("digest", help="digest planner: learnings -> reference/rule/howto/decision merge candidates")
    dgsub = dg.add_subparsers(dest="digest_action", required=True)
    dgp = dgsub.add_parser("plan", help="propose merge candidates for un-marked active learning sections")
    dgp.add_argument("--project", dest="filter_project",
                     help="comma-separated ids/groups/general (T04 resolver, FR-1)")
    dgp.add_argument("--json", action="store_true")
    dgm = dgsub.add_parser("mark", help="record learning section(s) as processed (won't reappear in plan)")
    dgm.add_argument("file", nargs="?", help="vault-relative source file (learnings/active/...)")
    dgm.add_argument("section", nargs="?", help="section heading, used together with <file>")
    dgm.add_argument("--all", action="store_true", help="mark every entry in the current plan")
    dgm.add_argument("--json", action="store_true")

    nt = sub.add_parser("nightly", help="orchestrated nightly run: index update -> "
                                        "digest/curator/config snapshot -> trend line -> markers sweep")
    nt.add_argument("--json", action="store_true")

    n = sub.add_parser("note", help="notes")
    nsub = n.add_subparsers(dest="note_action", required=True)
    nn = nsub.add_parser("new")
    nn.add_argument("type")
    nn.add_argument("title")
    nn.add_argument("--project", help="explicit project id/general/personal (else cwd-detected)")

    ll = sub.add_parser("llm", help="local inference server (llama-server + router)")
    llsub = ll.add_subparsers(dest="llm_action", required=True)
    lls = llsub.add_parser("start", help="start server (+router) for a model")
    lls.add_argument("model")
    llsub.add_parser("stop", help="stop server + router")
    llst = llsub.add_parser("status", help="running model, pids, health")
    llst.add_argument("--json", action="store_true")
    llsub.add_parser("list", help="models from config registry")
    lllog = llsub.add_parser("logs", help="tail server/router log")
    lllog.add_argument("which", nargs="?", choices=["server", "router"], default="server")
    lllog.add_argument("-n", type=int, default=40)
    llc = llsub.add_parser("code", help="run Claude Code on the local model")
    llc.add_argument("claude_args", nargs=argparse.REMAINDER,
                     help="extra args passed to claude (use -- before flags)")
    llset = llsub.add_parser("setup", help="verify/install local-LLM environment")
    llset.add_argument("--dir", default=None,
                       help="llama.cpp install dir (default: E:/llama.cpp)")

    l = sub.add_parser("learn", help="append an insight to active learnings")
    l.add_argument("insight")
    l.add_argument("--task", default="GENERAL")
    l.add_argument("--project", help="explicit project id/general/personal (else cwd-detected)")
    l.add_argument("--head", help="semantic heading appended after the timestamp")
    l.add_argument("--force", action="store_true", help="bypass dedup and always append")

    sub.add_parser("init", help="bind this vault (pointer file) + create folder tree")
    sub.add_parser("uninstall", help="remove hooks/scheduler jobs/daemon (vault untouched)")
    return p


# ---------------------------------------------------------------------------

def cmd_search(cfg, args) -> int:
    from . import projects as projects_mod
    from .search import client

    filter_project = None
    if args.filter_project:
        registry = cfg.get("projects", {}) or {}
        expanded: set[str] = set()
        for token in args.filter_project.split(","):
            token = token.strip()
            if not token:
                continue
            try:
                expanded.update(projects_mod.resolve_selector(registry, token))
            except KeyError as exc:
                print(f"[mb] {exc}", file=sys.stderr)
                return 1
        filter_project = sorted(expanded)

    payload = {
        "cmd": "search",
        "query": args.query,
        "top_k": args.top,
        "memory_only": args.memory_only,
        "code_only": args.code_only,
        "filter_type": args.filter_type,
        "filter_tags": args.filter_tags,
        "filter_wikilinks": args.filter_wikilinks,
        "filter_project": filter_project,
        "path_prefix": args.path_prefix,
        "full_content": args.full,
        "explain": args.explain,
        "max_per_file": args.max_per_file,
        "format": "json" if args.json else "compact",
    }
    resp = client.search(cfg, payload)
    if "error" in resp:
        print(f"[mb] error: {resp['error']}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(resp, ensure_ascii=False, indent=1))
    else:
        lines = resp.get("lines", [])
        if not lines:
            print("(no results)")
        for idx, line in enumerate(lines, 1):
            print(f"{idx}. {line}")
        cold = " COLD" if resp.get("cold") else ""
        print(f"-- {resp.get('time_ms', '?')}ms{cold}")
    return 0


def cmd_daemon(cfg, args) -> int:
    from .search import client

    if args.action == "run":
        from .search import daemon
        daemon.main(cfg)
        return 0
    if args.action == "start":
        ok = client.start_daemon(cfg)
        print("started" if ok else "FAILED to start — see .mb/daemon.log")
        return 0 if ok else 1
    if args.action == "stop":
        print("stopped" if client.stop_daemon(cfg) else "was not running")
        return 0
    if args.action == "reload":
        resp = client.reload_daemon(cfg)
        print(json.dumps(resp, ensure_ascii=False) if resp else "daemon not running")
        return 0 if resp else 1
    print(json.dumps(client.status(cfg), ensure_ascii=False, indent=1))
    return 0


def cmd_index(cfg, args) -> int:
    from .search import client, indexer

    if args.action == "inspect":
        target = args.path or (args.files[0] if args.files else None)
        if not target:
            print("[mb] usage: mb index inspect <vault-relative-file>", file=sys.stderr)
            return 1
        rows = indexer.inspect(cfg, target)
        print(json.dumps({"file": target, "chunks": rows}, ensure_ascii=False, indent=1))
        return 0

    # The indexer owns the writer lock now (so watcher/nightly are serialized
    # too, not just the CLI). Reload happens only after a successful commit.
    try:
        if args.action == "rebuild":
            stats = indexer.rebuild(cfg)
        else:
            stats = indexer.update(cfg, only_files=args.files or None)
    except indexer.IndexLockedError:
        print("[mb] another index writer in progress (lock present)", file=sys.stderr)
        return 1
    print(json.dumps(stats, ensure_ascii=False))
    client.reload_daemon(cfg)
    return 0


def cmd_backup(cfg, args) -> int:
    from . import backup

    if args.backup_action == "create":
        mf = backup.create(cfg)
        print(json.dumps(mf, ensure_ascii=False, indent=1))
        return 0
    if args.backup_action == "list":
        items = backup.list_backups(cfg)
        if args.json:
            print(json.dumps(items, ensure_ascii=False))
        else:
            for it in items:
                print(f"{it['id']}  machine={it.get('machine_id')}  "
                      f"gen={it.get('index_generation')}")
            if not items:
                print("(no backups)")
        return 0
    if args.backup_action == "verify":
        res = backup.verify(cfg, args.id)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    if args.backup_action == "restore":
        res = backup.restore(cfg, args.id)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res["ok"] else 1
    return 1


def cmd_config(cfg, args) -> int:
    from .config import validate_config

    if args.config_action == "validate":
        res = validate_config(cfg.data)
        if args.json:
            print(json.dumps({"ok": not res["errors"], **res,
                              "schema_version": cfg.get("schema_version")},
                             ensure_ascii=False))
        else:
            for e in res["errors"]:
                print(f"[error] {e}")
            for w in res["warnings"]:
                print(f"[warn] {w}")
            print("config OK" if not res["errors"] else "config has errors")
        return 1 if res["errors"] else 0

    if args.config_action == "get":
        print(json.dumps(cfg.get(args.key), ensure_ascii=False))
        return 0

    if args.config_action == "set":
        try:
            value = json.loads(args.value)     # parse as JSON (int/bool/list/...)
        except json.JSONDecodeError:
            value = args.value                 # fall back to a plain string
        candidate = {**cfg.data, args.key: value}
        res = validate_config(candidate)
        if res["errors"]:
            for e in res["errors"]:
                print(f"[mb] {e}", file=sys.stderr)
            return 1
        cfg.data = candidate
        cfg.save()                             # atomic (tmp + replace)
        print(f"{args.key} = {json.dumps(value, ensure_ascii=False)}")
        return 0
    return 1


def cmd_project(cfg, args) -> int:
    from pathlib import Path

    from . import projects
    from .config import validate_config

    entries = cfg.get("projects", {}) or {}

    if args.project_action == "list":
        shown = entries
        if args.group:
            try:
                ids = projects.resolve_selector(entries, args.group)
            except KeyError as exc:
                print(f"[mb] {exc}", file=sys.stderr)
                return 1
            shown = {pid: entries[pid] for pid in ids}
        if args.json:
            print(json.dumps(shown, ensure_ascii=False))
        else:
            if not shown:
                print("(no projects registered)")
            for pid, e in sorted(shown.items()):
                extra = "".join(f"  {k}={e[k]}" for k in ("group", "about") if e.get(k))
                print(f"{pid}: {e.get('path')}{extra}")
        return 0

    if args.project_action == "add":
        entry = {"path": args.path}
        if args.group:
            entry["group"] = args.group
        if args.about:
            entry["about"] = args.about
        candidate = {**cfg.data, "projects": {**entries, args.id: entry}}
        result = validate_config(candidate)
        if result["errors"]:
            for e in result["errors"]:
                print(f"[mb] {e}", file=sys.stderr)
            return 1
        cfg.data = candidate
        cfg.save()
        print(f"project added: {args.id} -> {entry['path']}")
        return 0

    if args.project_action == "remove":
        if args.id not in entries:
            print(f"[mb] unknown project id: {args.id} (known: "
                  f"{', '.join(sorted(entries)) or '(none)'})", file=sys.stderr)
            return 1
        remaining = {pid: e for pid, e in entries.items() if pid != args.id}
        cfg.data = {**cfg.data, "projects": remaining}
        cfg.save()
        print(f"project removed: {args.id}")
        return 0

    if args.project_action == "detect":
        target = args.path or str(Path.cwd())
        pid = projects.detect_project(entries, target)
        result = {"cwd": target, "project": pid or "general"}
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result["project"])
        return 0
    return 1


def cmd_hooks(cfg, args) -> int:
    from . import hooks_state

    if args.hooks_action == "status":
        data = hooks_state.status(cfg.vault)
        if args.json:
            print(json.dumps(data, ensure_ascii=False))
        else:
            desired = "enabled" if data["configured"] else "disabled"
            default = "enabled" if data["default"] else "disabled"
            print(f"capture hooks: {desired} (default: {default}, source: {data['source']})")
            for name, c in data["clients"].items():
                print(f"{name}: installed={'yes' if c['installed'] else 'no'}, "
                      f"mode={c['mode']}, effective={'yes' if c['effective'] else 'no'}")
            for w in data["warnings"]:
                print(f"[warn] {w}")
        return 0

    enabled = args.hooks_action == "enable"
    try:
        hooks_state.set_capture_enabled(cfg, enabled)
    except ValueError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    print(f"capture hooks {'enabled' if enabled else 'disabled'}")
    return 0


def cmd_digest(cfg, args) -> int:
    from . import digest
    from . import projects as projects_mod

    if args.digest_action == "plan":
        project_filter = None
        if getattr(args, "filter_project", None):
            registry = cfg.get("projects", {}) or {}
            expanded: set[str] = set()
            for token in args.filter_project.split(","):
                token = token.strip()
                if not token:
                    continue
                try:
                    expanded.update(projects_mod.resolve_selector(registry, token))
                except KeyError as exc:
                    print(f"[mb] {exc}", file=sys.stderr)
                    return 1
            project_filter = expanded

        result = digest.plan(cfg, project_filter=project_filter)
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"== mb digest plan -- index:{result['index_status']} "
                  f"sources:{result['source_count']} targets:{result['target_count']} ==")
            if not result["entries"]:
                print("(no candidates)")
            for e in result["entries"]:
                tag = f" @{e['project']}" if e["project"] else ""
                print(f"\n{e['file']} / {e['section']}{tag}")
                if not e["candidates"]:
                    print("   (no target match >= threshold)")
                for c in e["candidates"]:
                    ctag = f"@{c['project']}" if c["project"] else ""
                    print(f"   {c['score']:.2f}  {c['file']}  [{c['type']}{ctag}]")
        return 0

    if args.digest_action == "mark":
        if args.all:
            result = digest.plan(cfg)
            identities = [(e["file"], e["section"]) for e in result["entries"]]
        elif args.file and args.section:
            identities = [(args.file, args.section)]
        else:
            print("[mb] usage: mb digest mark <file> <section> | --all", file=sys.stderr)
            return 1
        count = digest.mark(cfg, identities)
        if args.json:
            print(json.dumps({"marked": count}, ensure_ascii=False))
        else:
            print(f"marked: {count}")
        return 0
    return 1


def cmd_nightly(cfg, args) -> int:
    from . import nightly

    result = nightly.run(cfg)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"== mb nightly {result['started_at']} ==")
        for name, s in result["steps"].items():
            extra = f" — {s['error']}" if s.get("error") else \
                    f" ({s['reason']})" if s.get("reason") else ""
            print(f"  {name}: {s.get('status')}{extra}")
    return 1 if nightly.has_error(result) else 0


def cmd_note(cfg, args) -> int:
    from . import capture

    folder_by_type = {"learning": "learnings/active", "decision": "decisions",
                      "howto": "howto", "incident": "incidents", "rule": "rules",
                      "reference": "reference", "daily": "daily"}
    folder = folder_by_type.get(args.type)
    if folder is None:
        print(f"[mb] unknown type: {args.type} (use {', '.join(folder_by_type)})",
              file=sys.stderr)
        return 1
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in args.title).strip()
    path = cfg.vault / folder / f"{safe}.md"
    if path.exists():
        print(f"[mb] already exists: {path}", file=sys.stderr)
        return 1

    dest_rel = str(path.relative_to(cfg.vault)).replace("\\", "/")
    project, err = capture.resolve_capture_project(cfg, args.project, dest_rel)
    if err:
        print(f"[mb] {err}", file=sys.stderr)
        return 1

    today = datetime.now().strftime("%Y-%m-%d")
    template = cfg.vault / "templates" / f"{args.type}.md"
    if template.exists():
        body = template.read_text(encoding="utf-8")
        body = (body.replace("{{title}}", args.title)
                    .replace("{{date}}", today).replace("{{type}}", args.type))
    else:
        body = (f"---\ntype: {args.type}\ntitle: \"{args.title}\"\ntags: []\n"
                f"created: {today}\nstatus: active\nsource: \"\"\nowner: user\n---\n\n"
                f"# {args.title}\n\n## Контекст\n\n\n## Суть\n\n")
    body = capture.with_project_frontmatter(body, project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(str(path))
    return 0


def cmd_learn(cfg, args) -> int:
    from .search.chunking import parse_frontmatter
    from . import capture

    path = cfg.vault / "learnings" / "active" / f"{args.task}.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")

    dest_rel = str(path.relative_to(cfg.vault)).replace("\\", "/")
    project, err = capture.resolve_capture_project(cfg, args.project, dest_rel)
    if err:
        print(f"[mb] {err}", file=sys.stderr)
        return 1

    existing_text = path.read_text(encoding="utf-8") if path.exists() else ""

    if not args.force and existing_text:
        dup = capture.find_duplicate(existing_text, args.insight)
        if dup:
            print(f"[mb] duplicate skipped (matches '{dup.heading}'): {path}")
            return 0

    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (f"---\ntype: learning\ntitle: \"{args.task}\"\ntags: []\n"
                f"created: {datetime.now().strftime('%Y-%m-%d')}\nstatus: active\n---\n\n"
                f"# {args.task}\n")
        body = capture.with_project_frontmatter(body, project)
        path.write_text(body, encoding="utf-8")
    elif project:
        existing_fm, _ = parse_frontmatter(existing_text)
        existing_project = existing_fm.get("project")
        if existing_project and existing_project != project:
            print(f"[mb] WARNING: appending to {path.name} (project={existing_project}) "
                  f"from a different project '{project}' — not changed", file=sys.stderr)

    heading = f"{stamp} — {args.head}" if args.head else stamp
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## {heading}\n{args.insight}\n")
    print(str(path))
    return 0


def cmd_llm(cfg, args) -> int:
    from . import llm

    if args.llm_action == "start":
        return llm.start(cfg, args.model)
    if args.llm_action == "stop":
        return llm.stop(cfg)
    if args.llm_action == "status":
        return llm.status(cfg, as_json=args.json)
    if args.llm_action == "list":
        return llm.list_models(cfg)
    if args.llm_action == "logs":
        return llm.logs(cfg, which=args.which, n=args.n)
    if args.llm_action == "code":
        return llm.code(cfg, args.claude_args)
    if args.llm_action == "setup":
        return llm.setup(cfg, dir_override=args.dir)
    return 2


def cmd_uninstall(cfg, _args) -> int:
    """Remove hooks (_mb-marked, plus pre-marker legacy entries recognized by
    their own command — [[Rule - hook ownership detection must tolerate
    pre-marker installs]]), scheduler jobs and the daemon. Vault untouched."""
    from pathlib import Path as _P

    import importlib.util

    from .platform import scheduler
    from .search import client

    _eng = _P(__file__).resolve().parents[1]

    def _load(name: str):
        spec = importlib.util.spec_from_file_location(name, _eng / "install" / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    settings_path = _P.home() / ".claude" / "settings.json"
    if settings_path.exists():
        _rh = _load("register_hooks")
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        removed = 0
        for event in list(hooks):
            before = len(hooks[event])
            hooks[event] = [e for e in hooks[event] if not _rh._is_owned(e, event)]
            removed += before - len(hooks[event])
            if not hooks[event]:
                del hooks[event]
        settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        print(f"hooks removed: {removed}")

    # Gemini artifacts (owned managed block, named hook, engine-pointing skill links)
    _rg = _load("register_gemini")
    g = _rg.uninstall()
    print(f"gemini: profile {g['profile']}, {g['hooks']} hook(s) removed, "
          f"{g['skills_removed']} skill link(s) removed, {g['skills_kept']} kept (foreign)")

    for job in (scheduler.Job("nightly-index", [], daily_at="03:00"),   # T02 legacy
                scheduler.Job("nightly", [], daily_at="03:00"),         # T12
                scheduler.Job("watch", [], at_login=True)):
        scheduler.unregister(job)
        print(f"scheduler job removed: {job.task_id}")
    print("daemon:", "stopped" if client.stop_daemon(cfg) else "was not running")
    print("vault untouched:", cfg.vault)
    return 0


def cmd_init(cfg, _args) -> int:
    write_pointer(cfg.vault)
    created = []
    for folder in MEMORY_FOLDERS:
        p = cfg.vault / folder
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(folder)
    cfg.save()
    print(f"vault bound: {cfg.vault}")
    if created:
        print(f"created folders: {', '.join(created)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage (cp1251) that cannot print
    # emoji/box chars coming from vault notes — force UTF-8, never crash on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    args = _build_parser().parse_args(argv)
    cfg = load_config(args.vault)
    if args.command == "search":
        return cmd_search(cfg, args)
    if args.command == "daemon":
        return cmd_daemon(cfg, args)
    if args.command == "index":
        return cmd_index(cfg, args)
    if args.command == "watch":
        from . import watch
        watch.run(cfg)
        return 0
    if args.command == "doctor":
        from . import doctor
        return doctor.run(cfg, fix=args.fix, heavy=args.heavy, as_json=args.json)
    if args.command == "curator":
        from . import curator
        return curator.run(cfg, as_json=args.json)
    if args.command == "selftest":
        from . import selftest
        return selftest.run(cfg, as_json=args.json)
    if args.command == "status":
        from . import status as status_mod
        return status_mod.run(cfg, as_json=args.json)
    if args.command == "stats":
        from . import stats as stats_mod
        return stats_mod.run(cfg, as_json=args.json)
    if args.command == "backup":
        return cmd_backup(cfg, args)
    if args.command == "config":
        return cmd_config(cfg, args)
    if args.command == "hooks":
        return cmd_hooks(cfg, args)
    if args.command == "project":
        return cmd_project(cfg, args)
    if args.command == "digest":
        return cmd_digest(cfg, args)
    if args.command == "nightly":
        return cmd_nightly(cfg, args)
    if args.command == "note":
        return cmd_note(cfg, args)
    if args.command == "learn":
        return cmd_learn(cfg, args)
    if args.command == "llm":
        return cmd_llm(cfg, args)
    if args.command == "init":
        return cmd_init(cfg, args)
    if args.command == "uninstall":
        return cmd_uninstall(cfg, args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
