"""mb — single CLI entry point (replaces the Genevra script sprawl).

mb search "<query>" [--top N] [--type T] [--tags a,b] [--links X] [--json] ...
mb daemon start|stop|restart|status|reload|run
mb index rebuild|update [--files f1 f2]|inspect <path>
mb watch
mb doctor [--fix] [--heavy] [--json]
mb curator [--json]
mb selftest [--json]
mb status [--json]
mb stats [--json]
mb backup create|list|verify|restore <id>
mb config validate|get <key>|set <key> <value>
mb note new <type> <title>
mb learn "<insight>" [--task ID]
mb hooks status|enable|disable [--json]
mb project list|add|remove|detect
mb digest plan [--project X] [--json]
mb digest mark <file> <section> | --all [--json]
mb ingest add <path>|--stdin [--title T] [--origin U] [--dry-run] [--json]
mb ingest list|show <id>|export <dir>|index [--rebuild]
mb nightly [--json]   # index update -> digest/curator/config snapshot -> trend -> markers sweep
mb reflect [--days N] [--json]   # privacy-safe adoption telemetry (offline transcript walk)
mb llm start|stop|status|list|logs|code|setup
mb init            # write ~/.singularity.json pointer + folder tree
mb uninstall       # remove hooks/scheduler jobs/daemon (vault untouched)
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
    s.add_argument("--with-code", action="store_true", dest="with_code",
                   help="also search the code corpus (.mb/code_index/) — code is "
                        "never in the default result set (CODE-INDEX D2)")
    s.add_argument("--repos", help="comma-separated repository names to keep, "
                                   "e.g. --repos api,web (code corpus only)")
    s.add_argument("--with-sources", action="store_true", dest="with_sources",
                   help="also search ingested raw sources (.mb/sources_index/) — "
                        "escalation when the wiki has no answer")
    s.add_argument("--full", action="store_true", help="full content, no excerpt cut")
    s.add_argument("--explain", action="store_true", help="include component scores")
    s.add_argument("--max-per-file", type=int, default=2,
                   help="cap results per file (0 = unlimited)")
    s.add_argument("--json", action="store_true")

    d = sub.add_parser("daemon", help="daemon control")
    d.add_argument("action",
                   choices=["start", "stop", "restart", "status", "reload", "run"])

    i = sub.add_parser("index", help="index management")
    i.add_argument("action", choices=["rebuild", "update", "inspect"])
    i.add_argument("--files", nargs="*", help="update only these vault-relative files")
    i.add_argument("--code", action="store_true",
                   help="act on the code corpus (.mb/code_index/) instead of "
                        "the vault — requires code_repos_dir")
    i.add_argument("path", nargs="?", help="file to inspect (index inspect <path>)")

    mdl = sub.add_parser("model", help="local embedding-model lifecycle")
    mdlsub = mdl.add_subparsers(dest="model_action", required=True)
    mdls = mdlsub.add_parser("status", help="verify the pinned local snapshot")
    mdls.add_argument("--json", action="store_true")
    mdli = mdlsub.add_parser("install", help="adopt/download the model once, then restart daemon")
    mdli.add_argument("--revision", help="optional Hugging Face revision to pin")
    mdli.add_argument("--force", action="store_true", help="re-resolve even with a healthy manifest")
    mdli.add_argument("--json", action="store_true")

    sub.add_parser("watch", help="on-save incremental index watcher")

    doc = sub.add_parser("doctor", help="installation verification")
    doc.add_argument("--fix", action="store_true")
    doc.add_argument("--heavy", action="store_true")
    doc.add_argument("--json", action="store_true",
                     help="emit a single JSON report, no human output")

    cur = sub.add_parser("curator", help="vault health: validation, orphans, link suggestions")
    cur.add_argument("--json", action="store_true")
    # OPTIONAL subcommands: bare `mb curator` must keep working, byte for byte.
    # Sub-flags use SUPPRESS so `mb curator --json contradictions` does not have
    # the subparser's default silently overwrite the parent's value.
    cursub = cur.add_subparsers(dest="curator_action")
    curc = cursub.add_parser("contradictions",
                             help="contradiction CANDIDATES (cosine band + lexical "
                                  "divergence signals) — not verdicts")
    curc.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    curc.add_argument("--limit", type=int, default=argparse.SUPPRESS)
    curc.add_argument("--same-file", action="store_true", dest="same_file",
                      help="also compare sections WITHIN one note (noisy: mostly "
                           "comparison tables and versioned recipes)")
    curg = cursub.add_parser("gaps", help="research agenda: what is MISSING "
                                          "(unprocessed sources, open questions, ...)")
    curg.add_argument("--top", type=int, default=argparse.SUPPRESS)
    curg.add_argument("--per-signal", type=int, dest="per_signal",
                      help="max items shown per signal (0 = no ceiling); "
                           "counts always report the true totals")
    curg.add_argument("--heuristic", action="store_true",
                      help="add frequency-based term guesses (off by default)")
    curg.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    curv = cursub.add_parser("verdict", help="record a verdict so a pair stops "
                                             "resurfacing (.mb/lint.json)")
    curv.add_argument("key", help="pair key from the contradictions report")
    curv.add_argument("--verdict", required=True,
                      choices=["false_positive", "resolved", "accepted"])
    curv.add_argument("--note", default="")
    curv.add_argument("--json", action="store_true", default=argparse.SUPPRESS)

    st = sub.add_parser("selftest", help="end-to-end pipeline check (create/index/find/delete)")
    st.add_argument("--json", action="store_true")

    stat = sub.add_parser("status", help="unified health snapshot (index/daemon/watcher/scheduler)")
    stat.add_argument("--json", action="store_true")

    stt = sub.add_parser("stats", help="memory analytics (notes/chunks/types/coverage)")
    stt.add_argument("--json", action="store_true")

    rfl = sub.add_parser("reflect", help="privacy-safe adoption telemetry "
                                         "(offline transcript walk, counts/rates only)")
    rfl.add_argument("--days", type=int, help="lookback window (default: reflect_window_days config)")
    rfl.add_argument("--json", action="store_true")

    bk = sub.add_parser("backup", help="backup/restore config + index pointer")
    bksub = bk.add_subparsers(dest="backup_action", required=True)
    bkc = bksub.add_parser("create", help="create a checksummed backup "
                                          "(config + ingest registry + raw sources)")
    bkc.add_argument("--with-sources", dest="with_sources", action="store_true",
                     default=None, help="always include raw sources, ignoring "
                                        "backup_max_sources_mb")
    bkc.add_argument("--no-sources", dest="with_sources", action="store_false",
                     help="never include raw sources")
    bkl = bksub.add_parser("list", help="list backups")
    bkl.add_argument("--json", action="store_true")
    bkv = bksub.add_parser("verify", help="verify a backup's checksums")
    bkv.add_argument("id")
    bkr = bksub.add_parser("restore", help="restore config + index pointer from a backup")
    bkr.add_argument("id")

    cfgp = sub.add_parser("config", help="inspect/validate the machine config")
    cfgsub = cfgp.add_subparsers(dest="config_action", required=True)
    cfgv = cfgsub.add_parser("validate", help="check schema (types/ranges/unknown keys)")
    cfgv.add_argument("--json", action="store_true")
    cfgg = cfgsub.add_parser("get", help="print a config value")
    cfgg.add_argument("key")
    cfgs = cfgsub.add_parser("set", help="set a config value (validated, atomic save)")
    cfgs.add_argument("key")
    cfgs.add_argument("value", help="JSON value; falls back to a string literal")
    cfgs.add_argument("--allow-unknown", action="store_true",
                      help="write a key the schema does not know (a typo is "
                           "refused without this)")

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

    ing = sub.add_parser("ingest", help="raw source layer: store external documents "
                                        "with provenance, plan wiki fan-out")
    ingsub = ing.add_subparsers(dest="ingest_action", required=True)
    inga = ingsub.add_parser("add", help="store one raw document (write-once, idempotent)")
    inga.add_argument("path", nargs="?", help="local file to ingest (or use --stdin)")
    inga.add_argument("--stdin", action="store_true", help="read the document from stdin")
    inga.add_argument("--title", help="explicit title (default: first H1 / first line)")
    inga.add_argument("--origin", help="where the document came from (URL or path) — "
                                       "recorded as provenance")
    inga.add_argument("--project", help="explicit project id/general/personal (else cwd-detected)")
    inga.add_argument("--tags", help="comma-separated tags")
    inga.add_argument("--content-type", default=None, help="MIME type (default: text/markdown)")
    inga.add_argument("--dry-run", action="store_true", help="print the intent, write nothing")
    inga.add_argument("--json", action="store_true")
    ingl = ingsub.add_parser("list", help="list registered sources (newest first)")
    ingl.add_argument("--status", choices=list(("new", "partial", "processed")))
    ingl.add_argument("--limit", type=int)
    ingl.add_argument("--json", action="store_true")
    ings = ingsub.add_parser("show", help="metadata + a bounded excerpt of one source")
    ings.add_argument("source_id")
    ings.add_argument("--excerpt", type=int, help="excerpt length in chars "
                                                  "(default: excerpt_chars config)")
    ings.add_argument("--json", action="store_true")
    inge = ingsub.add_parser("export", help="COPY raw sources + registry out of .mb/ "
                                            "(disaster escape hatch, never moves)")
    inge.add_argument("dir", help="destination directory")
    inge.add_argument("--id", dest="source_id", help="export a single source")
    inge.add_argument("--force", action="store_true", help="allow a non-empty destination")
    inge.add_argument("--json", action="store_true")
    ingi = ingsub.add_parser("index", help="build/catch up the sources corpus "
                                           "(the watcher does NOT see .mb/)")
    ingi.add_argument("--rebuild", action="store_true", help="full rebuild instead of update")
    ingi.add_argument("--json", action="store_true")
    ingb = ingsub.add_parser("backfill", help="retro-provenance: register the external "
                                              "documents existing notes already cite")
    ingb.add_argument("--apply", action="store_true",
                      help="write to the registry (default is a dry run)")
    ingb.add_argument("--json", action="store_true")
    ingp = ingsub.add_parser("plan", help="deterministic fan-out: which wiki pages "
                                          "this source should touch")
    ingp.add_argument("source_id")
    ingp.add_argument("--top-k", type=int, dest="top_k",
                      help="max pages to propose (default: ingest_fanout_top_k)")
    ingp.add_argument("--threshold", type=float,
                      help="min cosine (default: ingest_fanout_threshold)")
    ingp.add_argument("--summary-type", dest="summary_type", default="reference",
                      choices=["reference", "howto", "rule", "decision", "incident"],
                      help="note type proposed for the summary page")
    ingp.add_argument("--json", action="store_true")
    ingm = ingsub.add_parser("mark", help="record which notes a source produced "
                                          "(they stop resurfacing in plan)")
    ingm.add_argument("source_id")
    ingm.add_argument("--notes", help="comma-separated vault-relative note paths; "
                                      "omit for the legitimate 'nothing to add' outcome")
    ingm.add_argument("--status", choices=["new", "partial", "processed"],
                      default="processed")
    ingm.add_argument("--actor", default="manual", help="skill|manual (journal field)")
    ingm.add_argument("--json", action="store_true")
    ingg = ingsub.add_parser("log", help="read the machine history "
                                         "(.mb/ingest-log.jsonl)")
    ingg.add_argument("--since", help="ISO date/timestamp lower bound (YYYY-MM-DD)")
    ingg.add_argument("--op", help="filter by operation (ingest|query|lint)")
    ingg.add_argument("--limit", type=int, help="most recent N rows")
    ingg.add_argument("--json", action="store_true")

    nt = sub.add_parser("nightly", help="orchestrated nightly run: index update -> "
                                        "digest/curator/config snapshot -> trend line -> markers sweep")
    nt.add_argument("--json", action="store_true")

    n = sub.add_parser("note", help="notes")
    nsub = n.add_subparsers(dest="note_action", required=True)
    nn = nsub.add_parser("new")
    nn.add_argument("type")
    nn.add_argument("title")
    nn.add_argument("--project", help="explicit project id/general/personal (else cwd-detected)")
    nn.add_argument("--filename",
                    help="filename stem, when the vault's convention differs "
                         "from the title (e.g. `Rule - X.md` holding `title: X`)")
    nn.add_argument("--from-source", dest="from_source", action="append",
                    help="source_id this note is derived from (repeatable) — writes "
                         "`sources:` frontmatter and the registry back-link")

    ll = sub.add_parser("llm", help="local inference server (llama-server + router)")
    llsub = ll.add_subparsers(dest="llm_action", required=True)
    lls = llsub.add_parser("start", help="start server (+router) for a model")
    lls.add_argument("model")
    llsub.add_parser("stop", help="stop server + router")
    llst = llsub.add_parser("status", help="running model, pids, health")
    llst.add_argument("--json", action="store_true")
    llsub.add_parser("list", help="models from config registry")
    # `<name> <gguf>` positionally, mirroring `mb project add <id> <path>` —
    # the symmetry is the point, and it also keeps the REMAINDER last, where it
    # cannot swallow a required option. REMAINDER (not an option with nargs)
    # because llama-server args are all flag-shaped, and verbatim capture keeps
    # Windows paths intact where shlex-splitting one string would not.
    lladd = llsub.add_parser("add", help="register a model (validated, atomic)")
    lladd.add_argument("name")
    lladd.add_argument("gguf", help="path to the .gguf weights")
    lladd.add_argument("server_args", nargs=argparse.REMAINDER,
                       help="extra llama-server args (use -- before flags)")
    llrm = llsub.add_parser("remove", help="unregister a model")
    llrm.add_argument("name")
    llrm.add_argument("--force", action="store_true",
                      help="unregister even while it is the model being served")
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

    if args.with_sources and not cfg.get("ingest_index_sources"):
        print("[mb] --with-sources: the sources corpus is disabled "
              "(ingest_index_sources=false)", file=sys.stderr)
        print("[mb] enable it with `mb config set ingest_index_sources true`, "
              "then `mb ingest index --rebuild`", file=sys.stderr)
        return 2

    if (args.with_code or args.code_only) and cfg.code_repos_dir is None:
        print("[mb] the code corpus is off (code_repos_dir is not set)",
              file=sys.stderr)
        print('[mb] enable it with `mb config set code_repos_dir '
              '"<path/to/your/repos>"`, then `mb index rebuild --code`',
              file=sys.stderr)
        return 2

    payload = {
        "cmd": "search",
        "query": args.query,
        "top_k": args.top,
        "memory_only": args.memory_only,
        "code_only": args.code_only,
        "include_code": args.with_code,
        "repos": args.repos,
        "include_sources": args.with_sources,
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
    for key in ("sources_warning", "code_warning"):
        if resp.get(key) and not args.json:
            print(f"[mb] warning: {resp[key]}", file=sys.stderr)
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
    if args.action == "restart":
        ok = client.restart_daemon(cfg)
        print("restarted" if ok else "FAILED to start — see .mb/daemon.log")
        return 0 if ok else 1
    if args.action == "reload":
        resp = client.reload_daemon(cfg)
        print(json.dumps(resp, ensure_ascii=False) if resp else "daemon not running")
        if resp and resp.get("restart_required"):
            print(f"[mb] {', '.join(resp['restart_required'])} changed — "
                  f"reload cannot rebind it, run `mb daemon restart`",
                  file=sys.stderr)
        return 0 if resp else 1
    print(json.dumps(client.status(cfg), ensure_ascii=False, indent=1))
    return 0


def cmd_index(cfg, args) -> int:
    from .search import client, indexer

    corpus = "code" if getattr(args, "code", False) else None
    if corpus == "code" and cfg.code_repos_dir is None:
        # refuse BEFORE anything creates .mb/code_index/: an empty index reads
        # as "your code has no matches", which is a worse answer than an error
        print("[mb] --code: the code corpus is off (code_repos_dir is not set)",
              file=sys.stderr)
        print('[mb] point it at the directory that HOLDS your repositories: '
              '`mb config set code_repos_dir "<path/to/your/repos>"`',
              file=sys.stderr)
        return 2

    if args.action == "inspect":
        target = args.path or (args.files[0] if args.files else None)
        if not target:
            print("[mb] usage: mb index inspect <vault-relative-file>", file=sys.stderr)
            return 1
        rows = indexer.inspect(cfg, target, corpus=corpus)
        print(json.dumps({"file": target, "chunks": rows}, ensure_ascii=False, indent=1))
        return 0

    try:
        stats = client.run_index(cfg, args.action, args.files or None, corpus=corpus)
    except client.RemoteIndexError as exc:
        if exc.error_type == "IndexLockedError":
            print("[mb] another index writer in progress (lock present)", file=sys.stderr)
        elif exc.error_type == "CorpusRootUnavailable":
            print(f"[mb] {exc}", file=sys.stderr)
            return 2
        else:
            print(f"[mb] index failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"corpus": corpus or "memory", **stats}, ensure_ascii=False))
    return 0


def cmd_model(cfg, args) -> int:
    from .search import client, model_store

    if args.model_action == "status":
        state = model_store.status(cfg)
        if args.json:
            print(json.dumps(state, ensure_ascii=False))
        elif state["installed"]:
            print(f"installed: {state['model']} @ {state.get('revision', '?')}")
            print(f"snapshot: {state['snapshot']}")
        else:
            print(f"not installed: {state.get('reason', 'unknown reason')}")
            print(f"cache: {state.get('cache', cfg.hf_cache)}")
        return 0 if state["installed"] else 1

    try:
        result = model_store.install(cfg, revision=args.revision, force=args.force)
        client.stop_daemon(cfg)
        daemon_ok = client.start_daemon(cfg)
    except Exception as exc:  # explicit command: surface actionable install failure
        if args.json:
            print(json.dumps({"installed": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"[mb] model install failed: {exc}", file=sys.stderr)
        return 1
    result["daemon"] = "up" if daemon_ok else "failed"
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"model: {result.get('source', 'installed')} -> {result.get('snapshot', '?')}")
        print("daemon:", result["daemon"])
    return 0 if daemon_ok else 1


def cmd_backup(cfg, args) -> int:
    from . import backup

    if args.backup_action == "create":
        mf = backup.create(cfg, with_sources=args.with_sources)
        print(json.dumps(mf, ensure_ascii=False, indent=1))
        for warning in mf.get("warnings", []):
            print(f"[mb] warning: {warning}", file=sys.stderr)
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
        from .config import ConfigKeyError, set_config_key
        try:
            candidate = set_config_key(cfg.data, args.key, value)
        except ConfigKeyError as exc:
            print(f"[mb] {exc}", file=sys.stderr)
            return 1
        res = validate_config(candidate)
        if res["errors"]:
            for e in res["errors"]:
                print(f"[mb] {e}", file=sys.stderr)
            return 1
        # An unknown key is ignored at runtime, so writing one is a no-op the
        # user will read as a fix — and `modle` vs `model` is one keystroke.
        # Refusing by default (owner decision 2026-08-04) makes the typo loud;
        # `--allow-unknown` keeps the legitimate case (connectors, a key a newer
        # build reads) one explicit flag away.
        if any(w.startswith(f"{args.key}: unknown config key") for w in res["warnings"]):
            if not getattr(args, "allow_unknown", False):
                import difflib
                from .config import DEFAULT_CONFIG
                near = difflib.get_close_matches(args.key, DEFAULT_CONFIG, n=3, cutoff=0.7)
                hint = f" (did you mean: {', '.join(near)}?)" if near else ""
                print(f"[mb] {args.key}: unknown config key{hint}\n"
                      f"      nothing reads it — write it anyway with "
                      f"--allow-unknown", file=sys.stderr)
                return 1
            print(f"[mb] {args.key}: unknown config key — written as requested",
                  file=sys.stderr)
        cfg.data = candidate
        cfg.save()                             # atomic (tmp + replace)
        print(f"{args.key} = {json.dumps(value, ensure_ascii=False)}")
        # A running daemon still serves the value it started with, and silently
        # writing a value the serving process ignores is the trap this cost a
        # day to (issue #12) — `rebuild --code` kept reporting 0 files against
        # a config that was already correct on disk.
        from .search import client
        answered, live = client.daemon_value(cfg, args.key)
        if answered and live != value:
            print(f"[mb] a daemon is running and still serves "
                  f"{args.key} = {json.dumps(live, ensure_ascii=False)} — "
                  f"run `mb daemon reload` to pick this up"
                  + (", or `mb daemon restart` (the model cannot be rebound "
                     "in place)" if args.key == "model" else ""),
                  file=sys.stderr)
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
        # `add` doubles as the supported way to REPOINT a project after a move
        # between machines, so it merges: rebuilding the entry from the flags
        # alone dropped `group` and `about` without a word, and the loss only
        # surfaced later as a project missing from its group.
        previous = entries.get(args.id)
        entry = dict(previous) if isinstance(previous, dict) else {}
        entry["path"] = args.path
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
        verb = "updated" if isinstance(previous, dict) else "added"
        print(f"project {verb}: {args.id} -> {entry['path']}")
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


def _ingest_read_input(args) -> tuple[str, str, str]:
    """(text, origin_kind, origin_ref).

    Network never enters the core (WIKI-LOOP D4): a URL passed as <path> is
    refused with the exact command that does work, instead of being silently
    fetched by an engine that is meant to be offline and deterministic."""
    from pathlib import Path as _P

    from . import sources as sources_mod

    if args.stdin and args.path:
        raise ValueError("pass either <path> or --stdin, not both")
    if not args.stdin and not args.path:
        raise ValueError("nothing to ingest: pass a file path or --stdin")

    if args.stdin:
        raw = sys.stdin.buffer.read()
        text = raw.decode("utf-8-sig", errors="replace")
        return text, "stdin", str(args.origin or "")

    if sources_mod.is_url(args.path):
        raise ValueError(
            f"`mb ingest add` does not download: {args.path}\n"
            f"       the engine stays offline by design (decision D4). Fetch the "
            f"content yourself and pipe it in:\n"
            f"         <fetch the url> | mb ingest add --stdin --origin {args.path}")

    path = _P(args.path)
    if not path.is_file():
        raise OSError(f"not a file: {path}")
    # newline="" so CRLF documents are stored byte-for-byte as they arrived
    with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as fh:
        text = fh.read()
    return text, "file", str(args.origin or path.resolve())


def _ingest_catch_up_index(cfg, stored_path: str) -> str:
    """Best-effort incremental index of ONE freshly stored source.

    The watcher does not see `.mb/` (a direct consequence of D1), so nothing
    else would pick the document up until nightly. A cold daemon is left cold
    on purpose: paying a 10-20s model load inside `mb ingest add` would make
    the command unusable — the caller gets `pending` and runs `mb ingest index`."""
    if not cfg.get("ingest_index_sources"):
        return "disabled"
    from .search import client

    if not client.ping(cfg):
        return "pending"
    try:
        client.run_index(cfg, "update", files=[stored_path], corpus="sources")
    except (client.RemoteIndexError, ValueError):
        return "pending"
    return "indexed"


def cmd_ingest(cfg, args) -> int:
    from . import capture
    from . import sources as sources_mod

    try:
        if args.ingest_action == "add":
            try:
                text, origin_kind, origin_ref = _ingest_read_input(args)
            except ValueError as exc:
                print(f"[mb] {exc}", file=sys.stderr)
                return 2
            except OSError as exc:
                print(f"[mb] {exc}", file=sys.stderr)
                return 1

            project, err = capture.resolve_capture_project(cfg, args.project, None)
            if err:
                print(f"[mb] {err}", file=sys.stderr)
                return 2
            tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]

            result = sources_mod.add(
                cfg, text, title=args.title, origin_kind=origin_kind,
                origin_ref=origin_ref, project=project, tags=tags,
                content_type=args.content_type or sources_mod.DEFAULT_CONTENT_TYPE,
                dry_run=args.dry_run)

            if result["dry_run"] or result["duplicate"]:
                result["indexed"] = "skipped"
            else:
                result["indexed"] = _ingest_catch_up_index(cfg, result["stored_path"])

            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                what = "would store" if result["dry_run"] else \
                       "duplicate" if result["duplicate"] else "stored"
                print(f"{what}: {result['source_id']}  {result['stored_path']}")
                print(f"title: {result['title']}")
                if result.get("supersedes"):
                    print(f"supersedes: {result['supersedes']}")
                if result["indexed"] == "pending":
                    print("indexed: pending — run `mb ingest index` to make it searchable")
                elif result["indexed"] == "indexed":
                    print("indexed: yes")
            return 0

        if args.ingest_action == "index":
            if not cfg.get("ingest_index_sources"):
                print("[mb] sources indexing is disabled (ingest_index_sources=false); "
                      "enable it with `mb config set ingest_index_sources true`",
                      file=sys.stderr)
                return 2
            from .search import client
            try:
                stats = client.run_index(cfg, "rebuild" if args.rebuild else "update",
                                         corpus="sources")
            except client.RemoteIndexError as exc:
                if exc.error_type == "IndexLockedError":
                    print("[mb] another sources-index writer in progress (lock present)",
                          file=sys.stderr)
                else:
                    print(f"[mb] sources index failed: {exc}", file=sys.stderr)
                return 1
            print(json.dumps({"corpus": "sources", **stats}, ensure_ascii=False))
            return 0

        if args.ingest_action == "list":
            rows = sources_mod.list_sources(cfg, status=args.status, limit=args.limit)
            if args.json:
                print(json.dumps({"sources": rows}, ensure_ascii=False))
            elif not rows:
                print("(no sources)")
            else:
                for r in rows:
                    notes = len(r.get("derived_notes") or [])
                    print(f"{r['source_id']}  {r.get('status', '?'):9s} "
                          f"notes={notes:<3d} {r.get('ingested_at', '?')}  "
                          f"{r.get('title', '')}")
            return 0

        if args.ingest_action == "show":
            record = sources_mod.get(cfg, args.source_id)
            if record is None:
                print(f"[mb] unknown source_id: {args.source_id}", file=sys.stderr)
                return 2
            limit = args.excerpt if args.excerpt is not None else int(cfg.get("excerpt_chars"))
            body = sources_mod.read_source_text(cfg, args.source_id) or ""
            excerpt = body[:limit] + ("..." if len(body) > limit else "")
            if args.json:
                print(json.dumps({**record, "excerpt": excerpt,
                                  "excerpt_chars": limit}, ensure_ascii=False))
            else:
                print(f"id      : {record['source_id']}")
                print(f"title   : {record.get('title')}")
                print(f"status  : {record.get('status')}")
                print(f"origin  : {(record.get('origin') or {}).get('kind')} "
                      f"{(record.get('origin') or {}).get('ref')}")
                print(f"stored  : {record.get('stored_path')} "
                      f"({record.get('chars')} chars, {record.get('bytes')} bytes)")
                print(f"notes   : {', '.join(record.get('derived_notes') or []) or '(none)'}")
                print(f"--- excerpt ({limit} chars) ---")
                print(excerpt)
            return 0

        if args.ingest_action == "backfill":
            result = sources_mod.backfill(cfg, apply=args.apply)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                mode = "applied" if result["applied"] else "dry-run (nothing written)"
                print(f"== mb ingest backfill — {mode} ==")
                print(f"notes scanned : {result['notes_scanned']}")
                print(f"refs found    : {result['discovered']}")
                print(f"to create     : {result['created']}")
                print(f"to update     : {result['updated']}")
                for entry in result["entries"][:15]:
                    print(f"   {entry['source_id']}  {entry['ref'][:100]}")
                if result["capped_notes"]:
                    print(f"[warn] {len(result['capped_notes'])} note(s) hit the "
                          f"{sources_mod.BACKFILL_MAX_URLS_PER_NOTE}-URL cap: "
                          f"{', '.join(result['capped_notes'][:5])}")
                if not result["applied"] and (result["created"] or result["updated"]):
                    print("\nrun again with --apply to write these to the registry")
            return 0

        if args.ingest_action == "plan":
            from . import ingest_plan

            result = ingest_plan.plan(cfg, args.source_id, top_k=args.top_k,
                                      threshold=args.threshold,
                                      summary_type=args.summary_type)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                if result["error"]:
                    print(f"[mb] {result['error']}", file=sys.stderr)
                elif result["skipped"]:
                    print(f"[mb] skipped — {result['skipped']}")
                elif result["index_status"] != "ok" or result["sources_index_status"] != "ok":
                    print(f"[mb] index not ready — vault:{result['index_status']} "
                          f"sources:{result['sources_index_status']}", file=sys.stderr)
                else:
                    print(f"== mb ingest plan {result['source_id']} — "
                          f"{result['title']} ==")
                    st = result["summary_target"]
                    print(f"summary: {st['file']} "
                          f"({'exists' if st['exists'] else 'new'})")
                    print(f"         {st['command']}")
                    print(f"\ntouch ({len(result['touch'])}/{result['top_k']}, "
                          f"cosine>={result['threshold']}):")
                    if not result["touch"]:
                        print("   (no page above threshold — a legitimate outcome)")
                    for t in result["touch"]:
                        tag = f"@{t['project']}" if t["project"] else ""
                        print(f"   {t['score']:.2f}  {t['file']}  [{t['type']}{tag}]")
                        print(f"         why: «{t['why']['source_section']}» -> "
                              f"«{t['why']['note_section']}» ({t['why']['pair_score']:.2f})")
                    if result["new_pages"]:
                        print(f"\nnew pages ({len(result['new_pages'])}):")
                        for p in result["new_pages"]:
                            print(f"   [{p['reason']}] {p['name']}")
                    if result["conflicts"]:
                        print(f"\nconflict candidates ({len(result['conflicts'])}) "
                              f"— candidates, not verdicts:")
                        for c in result["conflicts"]:
                            print(f"   {c['score']:.2f}  {c['b']['file']} :: "
                                  f"{c['b']['section']}")
                            for sig, tokens in sorted(c["evidence"].items()):
                                print(f"         {sig}: {', '.join(tokens[:4])}")
            if result["error"]:
                return 2
            return 0

        if args.ingest_action == "mark":
            notes = [n.strip() for n in (args.notes or "").split(",") if n.strip()]
            result = sources_mod.attach_notes(
                cfg, args.source_id, notes, status=args.status,
                actor=args.actor, log=True)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"marked {result['source_id']}: status={result['status']}, "
                      f"+{len(result['added'])} note(s), "
                      f"{len(result['derived_notes'])} total")
                if not notes:
                    print("recorded outcome: nothing to add (a legitimate result)")
            return 0

        if args.ingest_action == "log":
            rows = sources_mod.read_log(cfg, since=args.since, op=args.op,
                                        limit=args.limit)
            if args.json:
                print(json.dumps({"rows": rows}, ensure_ascii=False))
            elif not rows:
                print("(empty log)")
            else:
                for row in rows:
                    notes = ", ".join(row.get("notes") or []) or "(none)"
                    print(f"{row.get('ts')}  {row.get('op')}  {row.get('source_id')}  "
                          f"[{row.get('actor')}] {row.get('status')}  {notes}")
            return 0

        if args.ingest_action == "export":
            from pathlib import Path as _P
            result = sources_mod.export(cfg, _P(args.dir),
                                        source_id=args.source_id, force=args.force)
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"exported {result['exported']} source(s) -> {result['dest']}")
                if result["missing"]:
                    print(f"[warn] {len(result['missing'])} registry entries had no raw "
                          f"file: {', '.join(result['missing'])}")
            return 0

    except sources_mod.SourceStateError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    except sources_mod.SourceError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[mb] i/o error: {exc}", file=sys.stderr)
        return 1
    return 2


def cmd_curator(cfg, args) -> int:
    """`mb curator` (unchanged) plus optional W07/W08 subcommands. The bare
    form must stay byte-identical — it is what nightly snapshots and what
    people have in their heads."""
    action = getattr(args, "curator_action", None)

    if action == "contradictions":
        from . import contradictions
        return contradictions.run(cfg, as_json=args.json,
                                  limit=getattr(args, "limit", None)
                                  or contradictions.DEFAULT_LIMIT,
                                  include_same_file=getattr(args, "same_file", False))

    if action == "gaps":
        from . import gaps
        per_signal = getattr(args, "per_signal", None)
        return gaps.run(cfg, as_json=args.json, top=getattr(args, "top", None),
                        heuristic=getattr(args, "heuristic", False),
                        per_signal=gaps.DEFAULT_PER_SIGNAL if per_signal is None
                        else per_signal)

    if action == "verdict":
        from . import contradictions
        try:
            result = contradictions.record_verdict(cfg, args.key, args.verdict,
                                                   note=args.note)
        except ValueError as exc:
            print(f"[mb] {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"verdict recorded: {result['verdict']} for {result['key']} "
                  f"({result['total']} total)")
        return 0

    from . import curator
    return curator.run(cfg, as_json=args.json)


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


def cmd_reflect(cfg, args) -> int:
    from . import reflect

    result = reflect.run(cfg, days=args.days)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(f"== mb reflect — last {result['window_days']}d, "
          f"{result['total_sessions']} top-level sessions "
          f"({', '.join(f'{k}={v}' for k, v in result['by_client'].items()) or 'none'}) ==")
    print(f"  by project: {', '.join(f'{k}={v}' for k, v in result['by_project'].items()) or 'none'}")
    for name, m in result["metrics"].items():
        rate = f"{m['rate']:.0%}" if m["rate"] is not None else "n/a"
        print(f"  {name:18s} {m['count']}/{m['total']}  ({rate})")
    if result["malformed_transcripts"]:
        print(f"malformed transcripts skipped: {result['malformed_transcripts']}")
    excluded = ", ".join(f"{k} ({v})" for k, v in result["excluded_clients"].items())
    print(f"excluded: {excluded}")
    return 0


def cmd_note(cfg, args) -> int:
    from . import capture
    from . import sources as sources_mod

    # Every managed knowledge root the vault guard watches needs a way to
    # create a file in it, or the guard is telling people to use a command that
    # does not exist and the workaround it warns about is the only option left.
    # `moc` and `changelog` have no template of their own and fall through to
    # the generic body below — that is fine; the point is that the sanctioned
    # path exists. `learnings/archive` is deliberately absent: notes ARRIVE
    # there by being moved, never by being created.
    # `tests/test_note_covers_managed_roots.py` holds this to MANAGED_ROOTS.
    folder_by_type = {"learning": "learnings/active", "decision": "decisions",
                      "howto": "howto", "incident": "incidents", "rule": "rules",
                      "reference": "reference", "daily": "daily",
                      "moc": "_moc", "changelog": "changelog"}
    folder = folder_by_type.get(args.type)
    if folder is None:
        print(f"[mb] unknown type: {args.type} (use {', '.join(folder_by_type)})",
              file=sys.stderr)
        return 1

    # W04 provenance: validate every id BEFORE creating the file, so a typo
    # never leaves a note claiming a source that does not exist.
    from_sources = list(getattr(args, "from_source", None) or [])
    for sid in from_sources:
        if sources_mod.get(cfg, sid) is None:
            print(f"[mb] unknown source_id: {sid} (see `mb ingest list`)",
                  file=sys.stderr)
            return 2
    # The vault's own convention has the type in the FILENAME and a clean
    # `title:` inside (`rules/Rule - X.md` + `title: X`), which a single
    # positional argument cannot express — so it got expressed by putting the
    # prefix in the title, where it does not belong. `--filename` separates the
    # two without changing what happens when it is absent.
    stem_source = args.filename if getattr(args, "filename", None) else args.title
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in stem_source).strip()
    if not safe:
        print(f"[mb] {stem_source!r} leaves nothing usable as a filename",
              file=sys.stderr)
        return 1
    path = cfg.vault / folder / f"{safe}.md"
    if path.exists():
        print(f"[mb] already exists: {path}", file=sys.stderr)
        return 1
    # The filename is the wikilink target, and it is what the sanitizer, not the
    # author, decided. A comma in a title becomes `_` — so `[[<title>]]` written
    # from memory in another note points at nothing, and the break surfaces
    # later as a missing page in `mb curator gaps` rather than here. Say it once,
    # at the only moment the difference is still cheap to fix.
    # With `--filename` the difference is the author's intent, not a surprise:
    # only announce the link target when it still resolves to something else.
    if safe != stem_source.strip():
        print(f"[mb] filename differs from the title: link to it as "
              f"[[{safe}]] (characters outside letters, digits, space, `-` "
              f"and `_` become `_`)", file=sys.stderr)

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
    body = capture.with_sources_frontmatter(body, from_sources)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    for sid in from_sources:
        sources_mod.attach_notes(cfg, sid, [dest_rel])
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
    if args.llm_action == "add":
        extra = list(args.server_args or [])
        if extra and extra[0] == "--":       # the separator, not an argument
            extra = extra[1:]
        return llm.add_model(cfg, args.name, args.gguf, extra)
    if args.llm_action == "remove":
        return llm.remove_model(cfg, args.name, force=args.force)
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

    # Grok artifacts (owned managed AGENTS.md block, engine-pointing skill links;
    # config.toml, sessions, credentials and foreign content are never touched)
    _rk = _load("register_grok")
    k = _rk.uninstall()
    print(f"grok: profile {k['profile']}, {k['hooks']} hook(s) removed, "
          f"{k['skills_removed']} skill link(s) removed, {k['skills_kept']} kept (foreign)")

    # PATH registration (issue #15): only the links that resolve back into this
    # engine, and only the PATH entry we added — a foreign `~/.local/bin/mb` or
    # someone else's PATH entry is reported and kept.
    _rp = _load("register_path")
    p = _rp.uninstall()
    if "path_entry" in p:
        print(f"path: user PATH entry {p['path_entry']}")
    else:
        print(f"path: {len(p['removed'])} link(s) removed, "
              f"{len(p['kept'])} kept (not ours)")

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
    if args.command == "model":
        return cmd_model(cfg, args)
    if args.command == "watch":
        from . import watch
        watch.run(cfg)
        return 0
    if args.command == "doctor":
        from . import doctor
        return doctor.run(cfg, fix=args.fix, heavy=args.heavy, as_json=args.json)
    if args.command == "curator":
        return cmd_curator(cfg, args)
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
    if args.command == "ingest":
        return cmd_ingest(cfg, args)
    if args.command == "nightly":
        return cmd_nightly(cfg, args)
    if args.command == "reflect":
        return cmd_reflect(cfg, args)
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
