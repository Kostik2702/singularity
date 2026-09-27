"""mb — single CLI entry point (replaces the earlier sprawl of separate scripts).

The window a human sees is ten commands wide and lives in `facade.py`:
search · learn · note · ingest · status · doctor · curator · llm · service ·
setup. Everything below is the full surface behind it — every direct path
listed here keeps working exactly as written, and the grouped forms
(`mb service daemon start`) are the same code reached through a rewritten
argv, never a second implementation.

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
mb usage [--session ID] [--list [--days N]] [--turns] [--top N] [--json]
                                 # per-step token cost + what each step added to the context
mb llm start|stop|status|list|logs|code|setup
mb code status|build [--all]|update|outline <file>|snippet|callers|callees|trace <symbol>|find <text>|schema|impact|arch|deadcode|sql|audit
                                 # the structural code graph (off until code_graph_enabled)
mb init            # write ~/.singularity.json pointer + folder tree
mb uninstall       # remove hooks/scheduler jobs/daemon (vault untouched)
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__, facade
from . import log as log_setup
from . import render
from .knowledge import inbox
from .commands.memory import cmd_search, cmd_ingest, cmd_note, cmd_learn, cmd_inbox
from .commands.knowledge import cmd_digest, cmd_curator, cmd_nightly
from .commands.machine import cmd_daemon, cmd_index, cmd_model, cmd_backup, cmd_config, cmd_project, cmd_hooks, cmd_reflect, cmd_usage, cmd_llm, cmd_uninstall, cmd_init, cmd_route
from .config import Config, ConfigError, load_config



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
    s.add_argument("--graph", action="store_true",
                   help="with --with-code/--code-only: add what the code graph knows "
                        "about each code hit — its definition, callers, callees — read "
                        "as last written, never refreshed (CODE-GRAPH D1)")
    s.add_argument("--graph-boost", action="store_true", dest="graph_boost",
                   help="re-rank code hits by how many callers the code graph knows "
                        "for their symbol (read-only; off by default, CODE-GRAPH P7)")
    s.add_argument("--with-sources", action="store_true", dest="with_sources",
                   help="also search ingested raw sources (.mb/sources_index/) — "
                        "escalation when the wiki has no answer")
    s.add_argument("--full", action="store_true", help="full content, no excerpt cut")
    s.add_argument("--explain", action="store_true", help="include component scores")
    s.add_argument("--max-per-file", type=int, default=2,
                   help="cap results per file (0 = unlimited)")
    s.add_argument("--hybrid", action="store_true", default=None,
                   help="blend BM25 with the vector ranking (RRF) — finds exact "
                        "tokens (identifiers, flags, filenames) a pure embedding "
                        "search can miss; config key: hybrid_search")
    s.add_argument("--sessions", action="store_true", dest="with_sessions",
                   help="also search your own past sessions (.mb/sessions.db) — "
                        "opt-in store, never in the default result set")
    s.add_argument("--json", action="store_true")

    ses = sub.add_parser("sessions", help="the opt-in store of your past sessions")
    sessub = ses.add_subparsers(dest="sessions_action", required=True)
    sesst = sessub.add_parser("status", help="rows, size, which clients are covered")
    sesst.add_argument("--json", action="store_true")
    sessub.add_parser("index", help="index sessions written since the last run")
    sessub.add_parser("purge", help="delete the store; transcripts and vault untouched")

    cg = sub.add_parser("code", help="the structural code graph: callers, callees, trace, outline")
    cgsub = cg.add_subparsers(dest="code_action", required=True)
    cgsub.add_parser("status", help="which repositories have a graph").add_argument("--json", action="store_true")
    cgb = cgsub.add_parser("build", help="parse and resolve this repository (or --all) into its graph")
    cgb.add_argument("--all", action="store_true", help="every unit under code_repos_dir and the extra roots")
    for verb, target_help in (("outline", "file, relative to the repository"),
                              ("snippet", "symbol: qname, Class.method or a unique name"),
                              ("callers", "symbol"), ("callees", "symbol"), ("trace", "symbol"),
                              ("find", "substring of a name or qname"), ("schema", None)):
        cgv = cgsub.add_parser(verb, help=f"{verb} ({target_help})" if target_help else verb)
        if target_help:
            cgv.add_argument("target")
        if verb == "trace":
            cgv.add_argument("--direction", choices=["callers", "callees"], default="callers")
            cgv.add_argument("--depth", type=int, default=3)
        cgv.add_argument("--limit", type=int, default=50)
        cgv.add_argument("--offset", type=int, default=0)
        cgv.add_argument("--no-refresh", action="store_true",
                         help="answer from the graph as last written, without comparing it with the "
                              "tree first (known stale files are still named)")
        cgv.add_argument("--json", action="store_true")
    cgu = cgsub.add_parser("update", help="bring the graph up to date with the tree, however much "
                                          "changed (what a query leaves to the background)")
    cgu.add_argument("--json", action="store_true")
    cgi = cgsub.add_parser("impact", help="what the change touches: changed symbols, their callers, the tests")
    cgi.add_argument("--base", help="compare with the merge-base of this ref and HEAD (default: HEAD, "
                                    "the uncommitted change)")
    cgi.add_argument("--depth", type=int, default=3)
    cga = cgsub.add_parser("arch", help="file communities, their coupling, the hotspots")
    cga.add_argument("--top", type=int, default=10)
    cgd = cgsub.add_parser("deadcode", help="definitions nothing calls, inherits, names or registers")
    cgd.add_argument("--unknown", action="store_true",
                     help="also list the candidates kept as unknown, each with its reason")
    cgau = cgsub.add_parser("audit", help="secret-shaped values stored in the graphs, redacted "
                                          "(every built graph, or --project's); exit 1 if any")
    cgau.add_argument("--json", action="store_true")
    for cgv in (cgi, cga, cgd):
        if cgv is not cga:
            cgv.add_argument("--limit", type=int, default=100 if cgv is cgi else 50)
            cgv.add_argument("--offset", type=int, default=0)
        cgv.add_argument("--no-refresh", action="store_true",
                         help="answer from the graph as last written, without comparing it with the tree first")
        cgv.add_argument("--json", action="store_true")
    for cgv in (cgb, cgu, cgi, cga, cgd, cgau, *[cgsub.choices[v] for v in ("outline", "snippet", "callers", "callees",
                                                                      "trace", "find", "schema")]):
        cgv.add_argument("--project", help="a registered project id instead of the current directory")
    cgb.add_argument("--json", action="store_true")
    # No positional: the SQL never passes through argv, where the shell would
    # have its say on quotes and `*` first (decision CODE-GRAPH D1).
    cgs = cgsub.add_parser("sql", help="one read-only SELECT over the v1_* views (`mb code schema` lists "
                                       "them); the SQL comes from --sql-file or stdin")
    cgs.add_argument("--sql-file", help="read the SQL from this file (default: stdin)")
    cgs.add_argument("--limit", type=int, default=100, help="rows at most; a cut answer says so")
    cgs.add_argument("--timeout", type=float, default=5.0, help="seconds the query may run before it is stopped")
    cgs.add_argument("--no-refresh", action="store_true",
                     help="answer from the graph as last written, without comparing it with the tree first")
    cgs.add_argument("--json", action="store_true")
    cgs.add_argument("--project", help="a registered project id instead of the current directory")

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

    rt = sub.add_parser("route", help="the chains of work, and the path to a phase")
    rt.add_argument("task", nargs="?", help=argparse.SUPPRESS)
    rt.add_argument("--class", dest="cls",
                    help="print one chain in full (dev, research, review, web, "
                         "image, memory, ops)")
    rt.add_argument("--phase", help="print the path to one phase file (5, SB, 1.5)")
    rt.add_argument("--json", action="store_true")

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

    jrn = sub.add_parser("journey", help="what the vault learned over a window "
                                         "(notes created + learning entries)")
    jrn.add_argument("--days", type=int, default=30)
    jrn.add_argument("--project", help="scope to one project id (else everything)")
    jrn.add_argument("--limit", type=int, default=60,
                     help="max entries printed; the counts above always cover the window")
    jrn.add_argument("--json", action="store_true")

    rfl = sub.add_parser("reflect", help="privacy-safe adoption telemetry "
                                         "(offline transcript walk, counts/rates only)")
    rfl.add_argument("--days", type=int, help="lookback window (default: reflect_window_days config)")
    rfl.add_argument("--json", action="store_true")

    usg = sub.add_parser("usage", help="token cost per step of a session, and what "
                                       "each step left in the context")
    usg.add_argument("--session", help="session id or a unique prefix (default: most recent)")
    usg.add_argument("--project", help="restrict to transcripts of one project dir")
    usg.add_argument("--list", action="store_true",
                     help="cost per session over a window instead of one session")
    usg.add_argument("--brief", action="store_true",
                     help="one line and nothing else — for closing a workflow phase")
    usg.add_argument("--from", type=int, default=0, dest="from_turn", metavar="TURN",
                     help="with --brief: count only turns from this index "
                          "(the previous phase's `next --from`)")
    usg.add_argument("--days", type=int, default=7, help="window for --list (default 7)")
    usg.add_argument("--turns", action="store_true", help="print every turn, not just the extremes")
    usg.add_argument("--top", type=int, default=12, help="rows per section (default 12)")
    usg.add_argument("--no-detail", dest="no_detail", action="store_true",
                     help="omit call targets (file paths, commands) — for sharing the report")
    usg.add_argument("--no-subagents", dest="no_subagents", action="store_true",
                     help="skip sidechain transcripts")
    usg.add_argument("--json", action="store_true")

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

    ing = sub.add_parser("ingest", help="store an external document with provenance")
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
    ingl.add_argument("--status", choices=["new", "partial", "processed"])
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

    n = sub.add_parser("note", help="create a note from its template")
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
    ns = nsub.add_parser("split",
                         help="split a note that outgrew its injection budget into "
                              "an index + one child note per `##` section")
    ns.add_argument("path", help="vault-relative path of the note to split")
    ns.add_argument("--dry-run", action="store_true",
                    help="show the plan, write nothing")

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

    lrn = sub.add_parser("learn", help="append an insight to active learnings")
    lrn.add_argument("insight")
    lrn.add_argument("--task", default="GENERAL")
    lrn.add_argument("--project", help="explicit project id/general/personal (else cwd-detected)")
    lrn.add_argument("--head", help="semantic heading appended after the timestamp")
    lrn.add_argument("--force", action="store_true", help="bypass dedup and always append")

    ib = sub.add_parser("inbox", help="staging area for captures not yet classified")
    ibsub = ib.add_subparsers(dest="inbox_action", required=True)
    ibc = ibsub.add_parser("add", help="capture text verbatim, decide later")
    ibc.add_argument("text")
    ibc.add_argument("--title")
    ibc.add_argument("--json", action="store_true")
    ibl = ibsub.add_parser("list", help="what is waiting, oldest first")
    ibl.add_argument("--json", action="store_true")
    ibs = ibsub.add_parser("suggest", help="type, duplicates and links — proposed, not applied")
    ibs.add_argument("file", help="inbox/<name>.md")
    ibs.add_argument("--json", action="store_true")
    iba = ibsub.add_parser("approve", help="file a capture into its managed root")
    iba.add_argument("file")
    iba.add_argument("type", choices=sorted(inbox.TYPE_ROOTS))
    iba.add_argument("--title")
    iba.add_argument("--json", action="store_true")

    sub.add_parser("init", help="bind this vault (pointer file) + create folder tree")
    sub.add_parser("uninstall", help="remove hooks/scheduler jobs/daemon (vault untouched)")
    return p


# ---------------------------------------------------------------------------









































def _load_config_or_report(vault: str | None) -> Config | None:
    """Config, or None after printing why — the CLI's half of `ConfigError`.

    `load_config` used to end the process itself; it now raises, because a
    daemon or a hook cannot recover from `sys.exit`. The user-visible result is
    unchanged: the same `[mb] …` lines on stderr, the same exit code 2."""
    try:
        return load_config(vault)
    except ConfigError as exc:
        for line in exc.lines:
            print(line, file=sys.stderr)
        return None


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy codepage (cp1251) that cannot print
    # emoji/box chars coming from vault notes — force UTF-8, never crash on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    # The facade owns the first screen: it rewrites group paths onto the real
    # commands and answers the four things argparse renders badly here (a bare
    # `mb`, the curated help, the full list, a group's own listing). Everything
    # it does not claim reaches the parser byte-identical to before.
    log_setup.configure_for_cli()      # notices go to stderr; reports are returned
    raw = list(sys.argv[1:] if argv is None else argv)
    plan = facade.resolve(raw)
    if plan.action == "help":
        print(facade.render_help(_build_parser(), full=plan.payload == "all"))
        return 0
    if plan.action == "group":
        print(facade.render_group(_build_parser(), plan.payload))
        return 0
    if plan.action == "error":
        print(plan.payload, file=sys.stderr)
        return 2
    if plan.action == "greeting":
        from .machine import status as status_mod
        cfg = _load_config_or_report(facade.vault_of(raw))
        if cfg is None:
            return 2
        print(facade.render_greeting(status_mod.snapshot(cfg)))
        return 0

    args = _build_parser().parse_args(plan.argv)
    # Before the config load on purpose: the chain of work lives in the engine,
    # so `mb route` must answer on a machine whose vault is missing, empty or
    # swapped — the same independence the agent preamble gained in v0.13.0.
    if args.command == "route":
        return cmd_route(args)
    cfg = _load_config_or_report(args.vault)
    if cfg is None:
        return 2
    if args.command == "inbox":
        return cmd_inbox(cfg, args)
    if args.command == "search":
        return cmd_search(cfg, args)
    if args.command == "daemon":
        return cmd_daemon(cfg, args)
    if args.command == "index":
        return cmd_index(cfg, args)
    if args.command == "model":
        return cmd_model(cfg, args)
    if args.command == "watch":
        from .machine import watch
        log_setup.configure_for_cli(stream=sys.stdout)   # watcher log, same as the daemon's
        watch.run(cfg)
        return 0
    if args.command == "doctor":
        from . import doctor
        return doctor.run(cfg, fix=args.fix, heavy=args.heavy, as_json=args.json)
    if args.command == "curator":
        return cmd_curator(cfg, args)
    if args.command == "selftest":
        from .machine import selftest
        return selftest.run(cfg, as_json=args.json)
    if args.command == "status":
        from .machine import status as status_mod
        snap = status_mod.snapshot(cfg)
        print(json.dumps(snap, ensure_ascii=False) if args.json else render.status(snap))
        return 0
    if args.command == "stats":
        from .knowledge import stats as stats_mod
        data = stats_mod.compute(cfg)
        print(json.dumps(data, ensure_ascii=False) if args.json else render.stats(data))
        return 0
    if args.command == "sessions":
        from .commands.memory import cmd_sessions
        return cmd_sessions(cfg, args)
    if args.command == "code":
        from .commands.code import cmd_code
        return cmd_code(cfg, args)
    if args.command == "journey":
        from .knowledge import journey as journey_mod
        data = journey_mod.collect(cfg, days=args.days, project=args.project)
        print(json.dumps(data, ensure_ascii=False) if args.json
              else render.journey(data, limit=args.limit))
        return 0
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
    if args.command == "usage":
        return cmd_usage(cfg, args)
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
