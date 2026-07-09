"""mb — single CLI entry point (replaces the Genevra script sprawl).

mb search "<query>" [--top N] [--type T] [--tags a,b] [--links X] [--json] ...
mb daemon start|stop|status|reload|run
mb index rebuild|update [--files f1 f2]
mb watch
mb doctor [--fix] [--heavy] [--json]
mb note new <type> <title>
mb learn "<insight>" [--task ID]
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
    s.add_argument("--path-prefix")
    s.add_argument("--memory-only", action="store_true")
    s.add_argument("--code-only", action="store_true")
    s.add_argument("--full", action="store_true", help="full content, no excerpt cut")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("daemon", help="daemon control")
    d.add_argument("action", choices=["start", "stop", "status", "reload", "run"])

    i = sub.add_parser("index", help="index management")
    i.add_argument("action", choices=["rebuild", "update"])
    i.add_argument("--files", nargs="*", help="update only these vault-relative files")

    sub.add_parser("watch", help="on-save incremental index watcher")

    doc = sub.add_parser("doctor", help="installation verification")
    doc.add_argument("--fix", action="store_true")
    doc.add_argument("--heavy", action="store_true")

    cur = sub.add_parser("curator", help="vault health: validation, orphans, link suggestions")
    cur.add_argument("--json", action="store_true")

    n = sub.add_parser("note", help="notes")
    nsub = n.add_subparsers(dest="note_action", required=True)
    nn = nsub.add_parser("new")
    nn.add_argument("type")
    nn.add_argument("title")

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

    sub.add_parser("init", help="bind this vault (pointer file) + create folder tree")
    sub.add_parser("uninstall", help="remove hooks/scheduler jobs/daemon (vault untouched)")
    return p


# ---------------------------------------------------------------------------

def cmd_search(cfg, args) -> int:
    from .search import client

    payload = {
        "cmd": "search",
        "query": args.query,
        "top_k": args.top,
        "memory_only": args.memory_only,
        "code_only": args.code_only,
        "filter_type": args.filter_type,
        "filter_tags": args.filter_tags,
        "filter_wikilinks": args.filter_wikilinks,
        "path_prefix": args.path_prefix,
        "full_content": args.full,
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

    lock = cfg.lock_dir
    try:
        lock.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        import time
        if time.time() - lock.stat().st_mtime < 6 * 3600:
            print("[mb] another rebuild in progress (lock present)", file=sys.stderr)
            return 1
        # stale lock -> take over (Genevra 6h rule)
    try:
        if args.action == "rebuild":
            stats = indexer.rebuild(cfg)
        else:
            stats = indexer.update(cfg, only_files=args.files or None)
        print(json.dumps(stats, ensure_ascii=False))
        client.reload_daemon(cfg)
        return 0
    finally:
        import shutil
        shutil.rmtree(lock, ignore_errors=True)


def cmd_note(cfg, args) -> int:
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    print(str(path))
    return 0


def cmd_learn(cfg, args) -> int:
    path = cfg.vault / "learnings" / "active" / f"{args.task}.md"
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"---\ntype: learning\ntitle: \"{args.task}\"\ntags: []\n"
            f"created: {datetime.now().strftime('%Y-%m-%d')}\nstatus: active\n---\n\n"
            f"# {args.task}\n", encoding="utf-8")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"\n## {stamp}\n{args.insight}\n")
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
    """Remove hooks (_mb-marked), scheduler jobs and the daemon. Vault untouched."""
    from pathlib import Path as _P

    from .platform import scheduler
    from .search import client

    settings_path = _P.home() / ".claude" / "settings.json"
    if settings_path.exists():
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = data.get("hooks", {})
        removed = 0
        for event in list(hooks):
            before = len(hooks[event])
            hooks[event] = [e for e in hooks[event] if not e.get("_mb")]
            removed += before - len(hooks[event])
            if not hooks[event]:
                del hooks[event]
        settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
        print(f"hooks removed: {removed}")
    for job in (scheduler.Job("nightly-index", [], daily_at="03:00"),
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
        return doctor.run(cfg, fix=args.fix, heavy=args.heavy)
    if args.command == "curator":
        from . import curator
        return curator.run(cfg, as_json=args.json)
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
