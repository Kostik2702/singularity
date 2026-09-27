"""Commands that put knowledge in and take it out: search, note, learn, ingest."""
from __future__ import annotations

import json
import sys
from datetime import datetime



def cmd_search(cfg, args) -> int:
    from ..machine import projects as projects_mod
    from ..search import client

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

    graph_boost = getattr(args, "graph_boost", False)
    if graph_boost and not (args.with_code or args.code_only):
        print("[mb] --graph-boost re-ranks code hits: add --with-code or --code-only",
              file=sys.stderr)
        return 2
    if graph_boost and not cfg.get("code_graph_enabled"):
        print("[mb] --graph-boost reads the code graph, which is off — "
              "`mb config set code_graph_enabled true`", file=sys.stderr)
        return 2

    payload = {
        "cmd": "search",
        "query": args.query,
        "top_k": args.top,
        # None (flag absent) leaves the decision to `hybrid_search` in config;
        # True forces it for this query only.
        "hybrid": getattr(args, "hybrid", None),
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
        # the boost needs the hits, not their rendering; it renders them itself
        "format": "json" if args.json or graph_boost else "compact",
    }
    resp = client.search(cfg, payload)
    if "error" in resp:
        print(f"[mb] error: {resp['error']}", file=sys.stderr)
        return 1
    if graph_boost:
        from ..search import graph_boost as boost
        resp = boost.apply(cfg, resp, explain=args.explain, as_lines=not args.json)

    # Sessions are a SEPARATE store queried separately — the same escalation
    # shape as --with-sources and --with-code, and the reason `mb search` with
    # no flags still returns exactly what it always returned.
    session_hits = []
    if getattr(args, "with_sessions", False):
        from ..telemetry import sessions as sessions_mod
        if not sessions_mod.enabled(cfg):
            print("[mb] --sessions: the session store is off "
                  "(`mb config set session_search_enabled true`)", file=sys.stderr)
        else:
            session_hits = sessions_mod.search(cfg, args.query, limit=args.top,
                                               project=None)
            resp["sessions"] = session_hits
    for key in ("sources_warning", "code_warning", "graph_boost_warning"):
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
        for hit in session_hits:
            body = " ".join(hit["body"].split())[:150]
            print(f"[session {hit['day']} {hit['role']}@{hit['project']}] {body}")
        cold = " COLD" if resp.get("cold") else ""
        print(f"-- {resp.get('time_ms', '?')}ms{cold}")
    return 0

def _ingest_read_input(args) -> tuple[str, str, str]:
    """(text, origin_kind, origin_ref).

    Network never enters the core (WIKI-LOOP D4): a URL passed as <path> is
    refused with the exact command that does work, instead of being silently
    fetched by an engine that is meant to be offline and deterministic."""
    from pathlib import Path as _P

    from ..knowledge import sources as sources_mod

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
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as fh:
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
    from ..search import client

    if not client.ping(cfg):
        return "pending"
    try:
        client.run_index(cfg, "update", files=[stored_path], corpus="sources")
    except (client.RemoteIndexError, ValueError):
        return "pending"
    return "indexed"

def cmd_ingest(cfg, args) -> int:
    from ..knowledge import capture
    from ..knowledge import sources as sources_mod

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
            from ..search import client
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
            from ..knowledge import ingest_plan

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

def _cmd_note_split(cfg, args) -> int:
    """`mb note split` — an over-budget note becomes an index plus children.

    Writes only after the whole plan is known to be safe: content preserved,
    no destination occupied. A partial split is the worst outcome of the three,
    because it leaves the vault in a state neither the author nor the tool can
    describe.
    """
    from ..knowledge import notesplit

    src = cfg.resolve(args.path)
    if not src.is_file():
        print(f"[mb] no such note: {args.path}", file=sys.stderr)
        return 1
    try:
        raw = src.read_text(encoding="utf-8-sig")
    except OSError as exc:
        print(f"[mb] cannot read {args.path}: {exc}", file=sys.stderr)
        return 1

    rel = str(src.relative_to(cfg.vault)).replace("\\", "/")
    budget = cfg.get("brain_max_lines", 40)
    plan = notesplit.plan(raw, rel, datetime.now().strftime("%Y-%m-%d"), budget)
    if not plan.ok:
        print(f"[mb] {plan.error}", file=sys.stderr)
        return 1

    if not notesplit.content_is_preserved(raw, plan):
        print("[mb] refusing to split: the plan would not preserve every line. "
              "This is a bug — please report it with the note that triggered it.",
              file=sys.stderr)
        return 2

    occupied = [c.rel_path for c in plan.children if cfg.resolve(c.rel_path).exists()]
    if occupied:
        print(f"[mb] {len(occupied)} destination(s) already exist; nothing written:",
              file=sys.stderr)
        for rel_path in occupied:
            print(f"   {rel_path}", file=sys.stderr)
        return 1

    print(f"{rel}: {plan.source_body_lines} lines -> "
          f"index of {plan.parent_body_lines} + {len(plan.children)} children "
          f"(brain_max_lines={budget})")
    for c in plan.children:
        tail = f" — {c.summary}" if c.summary else ""
        print(f"   {c.rel_path}{tail}")
    if args.dry_run:
        print("[mb] dry run — nothing written")
        return 0

    for c in plan.children:
        cfg.resolve(c.rel_path).write_text(c.text, encoding="utf-8")
    src.write_text(plan.parent_text, encoding="utf-8")
    if plan.parent_body_lines > budget:
        print(f"[mb] the index is still {plan.parent_body_lines} lines against a "
              f"{budget}-line budget — shorten the text above the section list",
              file=sys.stderr)
    return 0


def cmd_note(cfg, args) -> int:
    from ..knowledge import capture
    from ..knowledge import sources as sources_mod

    if getattr(args, "note_action", "new") == "split":
        return _cmd_note_split(cfg, args)

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
    safe = capture.safe_stem(stem_source)
    if not safe:
        print(f"[mb] {stem_source!r} leaves nothing usable as a filename",
              file=sys.stderr)
        return 1
    path = cfg.memory_folder(folder) / f"{safe}.md"
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
    template = cfg.templates_dir / f"{args.type}.md"
    if template.exists():
        body = template.read_text(encoding="utf-8")
        body = (body.replace("{{title}}", args.title)
                    .replace("{{date}}", today).replace("{{type}}", args.type))
    else:
        body = (f"---\ntype: {args.type}\ntitle: \"{args.title}\"\ntags: []\n"
                f"created: {today}\nstatus: active\nsource: \"\"\nowner: user\n---\n\n"
                f"# {args.title}\n\n## Context\n\n\n## Substance\n\n")
    body = capture.with_project_frontmatter(body, project)
    body = capture.with_sources_frontmatter(body, from_sources)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    for sid in from_sources:
        sources_mod.attach_notes(cfg, sid, [dest_rel])
    print(str(path))
    return 0

def cmd_learn(cfg, args) -> int:
    from ..search.chunking import parse_frontmatter
    from ..knowledge import capture, redact

    # Before dedup, before the file is even opened: the vault is a git
    # repository, so a key that reaches disk is in history on every machine the
    # vault has ever touched. Redaction also has to happen BEFORE dedup, or the
    # same insight captured twice — once raw, once masked — would read as two
    # different sections and both would be kept.
    original_insight = args.insight
    scrubbed = redact.redact(args.insight)
    args.insight = scrubbed.text
    # The notice is derived from what ACTUALLY changed, not from what the
    # sanitizer reported. If the assignment above is ever dropped, this goes
    # quiet instead of announcing a redaction that did not happen — a false
    # "redacted" is worse than silence, because it is read as proof.
    if args.insight != original_insight:
        for line in redact.format_notice(scrubbed.findings):
            print(f"[mb] {line}", file=sys.stderr)

    path = cfg.learnings_active_dir / f"{args.task}.md"
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


def cmd_inbox(cfg, args) -> int:
    """`mb inbox` — capture now, classify later, file only on approval.

    Every subcommand except `approve` is read-only by construction: the point
    of a staging area is that nothing leaves it unless a human says so.
    """
    from ..knowledge import inbox

    if not inbox.enabled(cfg):
        print("[mb] inbox is off — enable with `mb config set inbox_enabled true`",
              file=sys.stderr)
        return 2

    action = args.inbox_action
    if action == "add":
        result = inbox.capture(cfg, args.text, title=args.title)
        print(json.dumps(result, ensure_ascii=False) if args.json
              else f"captured: {result['file']}")
        return 0

    if action == "list":
        rows = inbox.items(cfg)
        if args.json:
            print(json.dumps({"items": rows}, ensure_ascii=False))
        elif not rows:
            print("inbox is empty")
        else:
            for row in rows:
                print(f"  {row['file']:<44} {row['title']}")
        return 0

    if action == "suggest":
        from ..search import client
        result = inbox.suggest(cfg, args.file,
                               search=lambda req: client.search(cfg, req))
        if result.get("error"):
            print(f"[mb] {result['error']}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 0
        print(f"{result['file']} — {result['title']}")
        print(f"  suggested type : {result['suggested_type']}  "
              f"(nothing is applied — run `mb inbox approve <file> <type>`)")
        for label, key in (("possible duplicates", "duplicates"), ("related", "links")):
            if result[key]:
                print(f"  {label}:")
                for hit in result[key]:
                    print(f"    {hit['score']:.2f}  {hit['file']}")
        return 0

    result = inbox.approve(cfg, args.file, args.type, title=args.title)
    if result.get("error"):
        print(f"[mb] {result['error']}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False) if args.json
          else f"filed: {result['from']} -> {result['file']}")
    return 0


def cmd_sessions(cfg, args) -> int:
    """`mb sessions` — the opt-in store of your own past conversations.

    Every subcommand states the enabled/disabled truth rather than acting as if
    an empty result meant "nothing was said": a store that is off and a store
    that is empty look identical from the outside, and confusing them is how
    someone concludes the feature is broken.
    """
    from ..telemetry import sessions

    if args.sessions_action == "status":
        snap = sessions.stats(cfg)
        if args.json:
            print(json.dumps(snap, ensure_ascii=False))
            return 0
        state = "on" if snap["enabled"] else "off (mb config set session_search_enabled true)"
        print(f"session search: {state}")
        print(f"store: {snap['messages']} messages, {snap['size_mb']} MB")
        print(f"covered clients: {', '.join(snap['covered_clients'])}"
              " — other clients are counted by `mb reflect` but not indexed here")
        return 0

    if args.sessions_action == "index":
        if not sessions.enabled(cfg):
            print("[mb] session search is off — `mb config set session_search_enabled true`",
                  file=sys.stderr)
            return 2
        result = sessions.store(cfg)
        print(f"indexed {result['indexed']} messages from {result['sessions']} session(s); "
              f"{result['redacted']} secret-shaped value(s) masked, "
              f"{result['pruned']} pruned past retention")
        return 0

    if args.sessions_action == "purge":
        print(sessions.purge(cfg)["status"])
        return 0

    print(f"[mb] unknown sessions action: {args.sessions_action}", file=sys.stderr)
    return 1
