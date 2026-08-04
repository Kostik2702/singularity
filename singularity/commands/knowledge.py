"""Commands that look after the knowledge already captured: curator, digest, nightly."""
from __future__ import annotations

import json
import sys
from .. import render



def cmd_digest(cfg, args) -> int:
    from .. import digest
    from .. import projects as projects_mod

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

def cmd_curator(cfg, args) -> int:
    """`mb curator` (unchanged) plus optional W07/W08 subcommands. The bare
    form must stay byte-identical — it is what nightly snapshots and what
    people have in their heads."""
    action = getattr(args, "curator_action", None)

    if action == "contradictions":
        from .. import contradictions
        return contradictions.run(cfg, as_json=args.json,
                                  limit=getattr(args, "limit", None)
                                  or contradictions.DEFAULT_LIMIT,
                                  include_same_file=getattr(args, "same_file", False))

    if action == "gaps":
        from .. import gaps
        per_signal = getattr(args, "per_signal", None)
        heuristic = getattr(args, "heuristic", False)
        result = gaps.report(cfg, top=getattr(args, "top", None), heuristic=heuristic,
                             per_signal=gaps.DEFAULT_PER_SIGNAL if per_signal is None
                             else per_signal)
        print(json.dumps(result, ensure_ascii=False, indent=1) if args.json
              else render.gaps(result, heuristic=heuristic))
        return 0

    if action == "verdict":
        from .. import contradictions
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

    from .. import curator
    payload = curator.collect(cfg)
    if args.json:
        print(json.dumps({"issues": payload["issues"], **payload["report"],
                          "lint": payload["lint"]}, ensure_ascii=False, indent=1))
    else:
        print(render.curator(payload,
                             orphan_source_days=curator.ORPHAN_SOURCE_DAYS,
                             stale_days=curator.STALE_DAYS,
                             suggest_threshold=curator.SUGGEST_THRESHOLD,
                             duplicate_threshold=curator.DUPLICATE_THRESHOLD,
                             supersession_gap_days=curator.SUPERSESSION_GAP_DAYS))
    return 1 if payload["issues"]["broken_links"] else 0

def cmd_nightly(cfg, args) -> int:
    from .. import nightly

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
