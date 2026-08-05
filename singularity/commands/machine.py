"""Commands about THIS machine: services, index, config, registry, install."""
from __future__ import annotations

import json
import sys
from .. import log as log_setup
from ..config import MEMORY_FOLDERS, write_pointer
from pathlib import Path



def cmd_daemon(cfg, args) -> int:
    from ..search import client

    if args.action == "run":
        # This process IS the daemon: its stdout is the log file the launcher
        # redirected, so notices belong there rather than on stderr.
        log_setup.configure_for_cli(stream=sys.stdout)
        from ..search import daemon
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
    from ..search import client, indexer

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
    from ..search import client, model_store

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
    from ..machine import backup

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
    from ..config import validate_config

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
        from ..config import ConfigKeyError, set_config_key
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
                from ..config import DEFAULT_CONFIG
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
        from ..search import client
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

    from ..machine import projects
    from ..config import validate_config

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
    from ..machine import hooks_state

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

def cmd_reflect(cfg, args) -> int:
    from ..telemetry import reflect

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

def cmd_llm(cfg, args) -> int:
    from .. import llm

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

    from ..platform import scheduler
    from ..search import client

    # parents[2], not [1]: this module moved one level down into `commands/`,
    # and the installer scripts live next to the PACKAGE, not inside it.
    _eng = _P(__file__).resolve().parents[2]

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
        p = cfg.memory_folder(folder)
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(folder)
    cfg.save()
    print(f"vault bound: {cfg.vault}")
    if created:
        print(f"created folders: {', '.join(created)}")
    return 0
