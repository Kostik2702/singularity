"""mb llm — supervise a local llama-server (+ optional router) for Claude Code.

State lives in <vault>/.mb/llm.state.json; process control is delegated to
singularity.platform.proc (cross-platform detached spawn/kill).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

from .platform import proc


class LLMConfigError(Exception):
    """Missing/invalid [llm] config section, a malformed field, or a
    referenced path is absent. The one error type every llm.py entry point
    converts user-facing config problems into — never a bare traceback."""


def _coerce_port(value, field: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise LLMConfigError(f"llm.{field} must be an integer port, got {value!r}") from None
    if not (0 < port < 65536):
        raise LLMConfigError(f"llm.{field} must be between 1 and 65535, got {port}")
    return port


def _coerce_positive_number(value, field: str):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise LLMConfigError(f"llm.{field} must be a number of seconds, got {value!r}") from None
    if n <= 0:
        raise LLMConfigError(f"llm.{field} must be > 0, got {n}")
    return n


def _require_object(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise LLMConfigError(f"{what} must be an object, got {type(value).__name__}")
    return value


def _validate_llm(data: dict) -> dict:
    """Type/shape-check the llm config section. Returns a shallow-copied,
    normalized dict (server_port/health_timeout_s/router.port coerced to
    numbers) — never mutates the caller's cfg.data in place. The single
    boundary every consumer (start/list/code/status) goes through, so a
    a malformed machine config always surfaces as one actionable
    LLMConfigError, never a KeyError/TypeError/ValueError traceback."""
    data = _require_object(data, "llm config section")
    data = dict(data)

    if "server_bin" in data and data["server_bin"] is not None \
            and not isinstance(data["server_bin"], str):
        raise LLMConfigError(f"llm.server_bin must be a string path, got {data['server_bin']!r}")
    if "server_host" in data and not isinstance(data["server_host"], str):
        raise LLMConfigError(f"llm.server_host must be a string, got {data['server_host']!r}")
    if "server_port" in data:
        data["server_port"] = _coerce_port(data["server_port"], "server_port")
    if "health_timeout_s" in data:
        data["health_timeout_s"] = _coerce_positive_number(data["health_timeout_s"], "health_timeout_s")

    models = data.get("models")
    if models is not None:
        models = _require_object(models, "llm.models")
        for name, spec in models.items():
            spec = _require_object(spec, f"llm.models.{name}")
            if "gguf" in spec and spec["gguf"] is not None and not isinstance(spec["gguf"], str):
                raise LLMConfigError(f"llm.models.{name}.gguf must be a string path, got {spec['gguf']!r}")
            args = spec.get("args")
            if args is not None and not isinstance(args, list):
                raise LLMConfigError(f"llm.models.{name}.args must be a list, got {type(args).__name__}")

    router = data.get("router")
    if router is not None:
        router = dict(_require_object(router, "llm.router"))
        cmd = router.get("cmd")
        if cmd is not None and not isinstance(cmd, list):
            raise LLMConfigError(f"llm.router.cmd must be a list, got {type(cmd).__name__}")
        if "port" in router:
            router["port"] = _coerce_port(router["port"], "router.port")
        data["router"] = router

    return data


def _llm_cfg(cfg) -> dict:
    data = cfg.get("llm")
    if not data:
        raise LLMConfigError(
            'no [llm] config — add an "llm" section to the machine config (~/.singularity.json) '
            "(server_bin + models)")
    return _validate_llm(data)


def build_server_argv(llm: dict, model_name: str) -> list[str]:
    server_bin = llm.get("server_bin")
    if not server_bin:
        raise LLMConfigError("llm.server_bin not set in config")
    models = llm.get("models") or {}
    model = models.get(model_name)
    if model is None:
        avail = ", ".join(models) or "(none)"
        raise LLMConfigError(f"unknown model '{model_name}' (have: {avail})")
    gguf = model.get("gguf")
    if not gguf:
        raise LLMConfigError(f"model '{model_name}' has no gguf path")
    host = llm.get("server_host", "127.0.0.1")
    port = _coerce_port(llm.get("server_port", 8080), "server_port")
    args = model.get("args") or []
    return [str(server_bin), "-m", str(gguf),
            "--host", str(host), "--port", str(port), *[str(a) for a in args]]


def validate_paths(llm: dict, model_name: str) -> None:
    server_bin_raw = llm.get("server_bin")
    if not server_bin_raw:
        raise LLMConfigError("llm.server_bin not set in config")
    server_bin = Path(server_bin_raw)
    if not server_bin.exists():
        raise LLMConfigError(f"server_bin not found: {server_bin}")
    models = llm.get("models") or {}
    model = models.get(model_name)
    if model is None:
        raise LLMConfigError(f"unknown model '{model_name}'")
    gguf_raw = model.get("gguf")
    if not gguf_raw:
        raise LLMConfigError(f"model '{model_name}' has no gguf path")
    gguf = Path(gguf_raw)
    if not gguf.exists():
        raise LLMConfigError(f"gguf not found: {gguf}")


def has_chat_template(llm: dict, model_name: str) -> bool:
    models = llm.get("models") or {}
    model = models.get(model_name) or {}
    args = model.get("args") or []
    return any(a == "--jinja" or str(a).startswith("--chat-template") for a in args)


def state_path(cfg) -> Path:
    return cfg.mb_dir / "llm.state.json"


def _log(cfg, name: str) -> Path:
    return cfg.mb_dir / f"llm-{name}.log"


def load_state(cfg) -> dict | None:
    try:
        return json.loads(state_path(cfg).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_state(cfg, state: dict) -> None:
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    state_path(cfg).write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def clear_state(cfg) -> None:
    state_path(cfg).unlink(missing_ok=True)


def poll_health(url: str, timeout_s: float, interval: float = 1.0,
                 pid: int | None = None) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        if pid is not None and not proc.is_running(pid):
            return False
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


def stop(cfg) -> int:
    """Stop the router (if any) then the server; clear state. Idempotent."""
    state = load_state(cfg)
    if not state:
        print("not running")
        return 0
    if state.get("router_pid"):
        proc.kill(state["router_pid"])
    if state.get("server_pid"):
        proc.kill(state["server_pid"])
    clear_state(cfg)
    print("stopped")
    return 0


def _start_lock_dir(cfg) -> Path:
    return cfg.mb_dir / "llm-start.lock"


def _acquire_start_lock(cfg, stale_after_s: float) -> bool:
    """Atomic mkdir-based lock (same pattern as cfg.lock_dir for index rebuild).
    Returns True if acquired (fresh or stale takeover), False if another start
    is genuinely in progress — caller must refuse to run."""
    lock = _start_lock_dir(cfg)
    try:
        lock.mkdir(parents=True, exist_ok=False)
        return True
    except FileExistsError:
        if time.time() - lock.stat().st_mtime < stale_after_s:
            return False
        print(f"[mb] stale start-lock ({lock}) — taking over", file=sys.stderr)
        return True


def start(cfg, model_name: str) -> int:
    try:
        llm = _llm_cfg(cfg)
        argv = build_server_argv(llm, model_name)
        validate_paths(llm, model_name)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1

    if not has_chat_template(llm, model_name):
        print("[mb] warning: model args lack --jinja/--chat-template; "
              "tool-calling may not work", file=sys.stderr)

    timeout = float(llm.get("health_timeout_s", 120))
    host = llm.get("server_host", "127.0.0.1")
    port = int(llm.get("server_port", 8080))
    ready = llm.get("server_ready_path", "/health")

    # serialize concurrent `mb llm start` — two racing starts would both spawn
    # onto the same port/VRAM. Stale window covers the worst case: two
    # sequential health-timeouts (server + router) plus margin.
    if not _acquire_start_lock(cfg, stale_after_s=2 * timeout + 60):
        print("[mb] another 'mb llm start' is already in progress — wait for it "
              "or 'mb llm stop' if it's stuck", file=sys.stderr)
        return 1

    spawned: list[int] = []      # PIDs *this run* created; cleanup scope on interrupt/crash
    try:
        # switch: free VRAM by stopping any live server first
        existing = load_state(cfg)
        if existing and (proc.is_running(existing.get("server_pid") or -1)
                          or proc.is_running(existing.get("router_pid") or -1)):
            print(f"[mb] stopping active model '{existing.get('model')}' first")
            stop(cfg)

        try:
            server_pid = proc.spawn_detached(argv, _log(cfg, "server"))
        except Exception as exc:
            print(f"[mb] failed to start server: {exc}", file=sys.stderr)
            clear_state(cfg)
            return 1
        spawned.append(server_pid)
        # fix the state the instant a PID exists, before waiting on health —
        # so an interrupt/crash during the health poll still leaves a
        # findable PID (for 'mb llm stop'/doctor) instead of a silent orphan.
        write_state(cfg, {"status": "starting", "model": model_name,
                          "server_pid": server_pid, "server_port": port,
                          "router_pid": None, "router_port": None,
                          "started_at": datetime.now().isoformat(timespec="seconds")})

        if not poll_health(f"http://{host}:{port}{ready}", timeout, pid=server_pid):
            proc.kill(server_pid)
            clear_state(cfg)
            print(f"[mb] server not healthy in {timeout:g}s — see {_log(cfg, 'server')}",
                  file=sys.stderr)
            return 1

        state = {
            "model": model_name,
            "server_pid": server_pid,
            "server_port": port,
            "router_pid": None,
            "router_port": None,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }

        router = llm.get("router")
        if router:
            r_host = router.get("host", "127.0.0.1")
            r_port = int(router.get("port", 3456))
            r_ready = router.get("ready_path", "/health")
            try:
                r_cmd = router.get("cmd")
                if not r_cmd:
                    raise LLMConfigError("router.cmd not set")
                router_pid = proc.spawn_detached([str(a) for a in r_cmd],
                                                 _log(cfg, "router"))
            except Exception as exc:
                print(f"[mb] failed to start router: {exc}", file=sys.stderr)
                proc.kill(server_pid)
                clear_state(cfg)
                return 1
            spawned.append(router_pid)
            write_state(cfg, {**state, "status": "starting",
                              "router_pid": router_pid, "router_port": r_port})

            if not poll_health(f"http://{r_host}:{r_port}{r_ready}", timeout, pid=router_pid):
                proc.kill(router_pid)
                proc.kill(server_pid)
                clear_state(cfg)
                print(f"[mb] router not healthy in {timeout:g}s — see {_log(cfg, 'router')}",
                      file=sys.stderr)
                return 1
            state["router_pid"] = router_pid
            state["router_port"] = r_port

        write_state(cfg, {**state, "status": "running"})
        print(f"[mb] started '{model_name}'")
        if router:
            print(f"export ANTHROPIC_BASE_URL=http://{router.get('host', '127.0.0.1')}:"
                  f"{int(router.get('port', 3456))}")
        else:
            print(f"OpenAI-compatible endpoint: http://{host}:{port}/v1")
            print("Run Claude Code on it: mb llm code")
        return 0
    except BaseException as exc:
        # interrupted (Ctrl+C) or crashed anywhere after a spawn: kill only the
        # PID(s) *this run* created — never touch state/processes belonging to
        # some other instance — and never leave a half-written state file.
        for pid in spawned:
            proc.kill(pid)
        if spawned:
            clear_state(cfg)
        if isinstance(exc, KeyboardInterrupt):
            print("[mb] start interrupted — cleaned up spawned process(es)",
                  file=sys.stderr)
            return 130
        raise
    finally:
        shutil.rmtree(_start_lock_dir(cfg), ignore_errors=True)


def status(cfg, as_json: bool = False) -> int:
    """--json is a stable machine contract: ALWAYS valid, parseable JSON, in
    both branches — including "no state at all", which used to print the
    plain string "not running" and break any consumer's json.loads(). Callers
    should key off "running" (== the server process is actually alive, not
    just "a state file exists")."""
    state = load_state(cfg)
    if not state:
        if as_json:
            print(json.dumps({"running": False, "server_alive": False,
                              "router_alive": None, "model": None},
                             ensure_ascii=False))
            return 0
        print("not running")
        return 0
    server_alive = proc.is_running(state.get("server_pid") or -1)
    router_alive = (proc.is_running(state["router_pid"])
                    if state.get("router_pid") else None)
    if as_json:
        print(json.dumps({**state, "running": server_alive,
                          "server_alive": server_alive, "router_alive": router_alive},
                         ensure_ascii=False, indent=2))
        return 0
    print(f"model:  {state.get('model')}")
    print(f"server: pid {state.get('server_pid')} "
          f"({'alive' if server_alive else 'DEAD'}) port {state.get('server_port')}")
    if state.get("router_pid"):
        print(f"router: pid {state['router_pid']} "
              f"({'alive' if router_alive else 'DEAD'}) port {state.get('router_port')}")
    print(f"since:  {state.get('started_at')}")
    if not server_alive:
        print("[mb] server pid is dead — run 'mb llm stop' to clear stale state")
    return 0


def add_model(cfg, name: str, gguf: str, extra_args: list[str] | None = None) -> int:
    """Register a model in `llm.models` — the write half `list` never had.

    Until v0.7.0 the registry was read-only and `mb llm list` told people to
    "add to llm.models in .mb/config.json" by hand: the exact edit the vault
    guard flags, in the file `mb config set` owns. Worse on the other end —
    `doctor A6-config-paths` reports dead `llm.models.*.gguf` after a move
    between machines and has nothing to offer for removing one.

    The path is NOT required to exist: a config is often prepared before the
    weights are downloaded, and A6 already reports a path that resolves
    nowhere. Say so and register anyway."""
    from .config import validate_config

    if not name.strip():
        print("[mb] model name must not be empty", file=sys.stderr)
        return 1
    llm_section = dict(cfg.get("llm") or {})
    models = dict(llm_section.get("models") or {})
    previous = models.get(name)
    spec = dict(previous) if isinstance(previous, dict) else {}
    spec["gguf"] = gguf
    if extra_args:
        spec["args"] = list(extra_args)
    models[name] = spec
    llm_section["models"] = models
    candidate = {**cfg.data, "llm": llm_section}

    try:
        _validate_llm(llm_section)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    result = validate_config(candidate)
    if result["errors"]:
        for e in result["errors"]:
            print(f"[mb] {e}", file=sys.stderr)
        return 1

    cfg.data = candidate
    cfg.save()
    print(f"model {'updated' if previous else 'added'}: {name} -> {gguf}")
    if not Path(gguf).exists():
        print(f"[mb] note: {gguf} does not exist on this machine yet "
              f"(`mb doctor` reports it under A6-config-paths)", file=sys.stderr)
    _warn_daemon_stale(cfg)
    return 0


def remove_model(cfg, name: str, force: bool = False) -> int:
    """Unregister a model. Refuses while it is the one being served, unless
    `--force`: stopping someone's running server as a side effect of editing a
    registry is not a config edit, it is an outage."""
    from .config import validate_config

    llm_section = dict(cfg.get("llm") or {})
    models = dict(llm_section.get("models") or {})
    if name not in models:
        print(f"[mb] unknown model: {name} (known: "
              f"{', '.join(sorted(models)) or '(none)'})", file=sys.stderr)
        return 1

    state = load_state(cfg) or {}
    if state.get("model") == name and not force:
        print(f"[mb] {name} is the model currently being served (pid "
              f"{state.get('server_pid')}) — `mb llm stop` first, or pass "
              f"--force to unregister it and leave the server running",
              file=sys.stderr)
        return 1

    del models[name]
    llm_section["models"] = models
    candidate = {**cfg.data, "llm": llm_section}
    result = validate_config(candidate)
    if result["errors"]:
        for e in result["errors"]:
            print(f"[mb] {e}", file=sys.stderr)
        return 1
    cfg.data = candidate
    cfg.save()
    print(f"model removed: {name}")
    if state.get("model") == name:
        print("[mb] the server for it is still running — `mb llm stop` when done",
              file=sys.stderr)
    _warn_daemon_stale(cfg)
    return 0


def _warn_daemon_stale(cfg) -> None:
    """Same courtesy `mb config set` pays (issue #12): a live daemon serves the
    config it started with, so a registry write it has not seen is a trap."""
    try:
        from .search import client
        answered, live = client.daemon_value(cfg, "llm")
    except Exception:                        # noqa: BLE001 — advisory only
        return
    if answered and live != cfg.get("llm"):
        print("[mb] a daemon is running and still serves the previous llm "
              "section — run `mb daemon reload`", file=sys.stderr)


def list_models(cfg) -> int:
    try:
        llm = _llm_cfg(cfg)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    models = llm.get("models") or {}
    if not models:
        print("[mb] no models in config — register one with "
              '`mb llm add <name> --gguf <path>`')
        return 1
    state = load_state(cfg)
    active = state.get("model") if state else None
    for name, spec in models.items():
        mark = " (active)" if name == active else ""
        print(f"{name}{mark}  {spec.get('gguf')}")
    return 0


def logs(cfg, which: str = "server", n: int = 40) -> int:
    path = _log(cfg, which)
    if not path.exists():
        print(f"[mb] no log yet: {path}")
        return 0
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-n:]:
        print(line)
    return 0


# --- mb llm code -----------------------------------------------------------

DEFAULT_AUTH_TOKEN = "local"  # dummy; local server without --api-key ignores auth


def resolve_auth_token(llm: dict, environ) -> str:
    """env ANTHROPIC_AUTH_TOKEN > config llm.auth_token > "local" (first non-empty)."""
    return (environ.get("ANTHROPIC_AUTH_TOKEN")
            or llm.get("auth_token")
            or DEFAULT_AUTH_TOKEN)


# --- isolated config profile (CLAUDE_CONFIG_DIR) ----------------------------
# A local model runs Claude Code far worse than Claude does. Instead of --bare
# (which drops skills/agents too), we point CLAUDE_CONFIG_DIR at a generated
# profile — a FULL replacement of ~/.claude, no merge back (verified via
# claude-code-guide) — that keeps skills/agents but strips what breaks a small
# model:
#   - hooks: the Singularity SessionStart hook injects context as an extra
#     role:system message, and Claude Code's own requests already carry a
#     non-first system message that trips strict jinja guards. (Hooks MERGE
#     across scopes and can't be disabled per-hook from the CLI; disableAllHooks
#     proved unreliable here.)
#   - enabledPlugins / mcpServers: Claude-only, and MCP tool-use overloads 9B.
#   - the full user CLAUDE.md: its "MANDATORY invoke skill X" / shortcut
#     directives send a small model into tool-call loops (observed an endless
#     TaskOutput(mb-search)/workflow-helper loop). We ship a minimal instruction
#     file instead, or a user-supplied one via llm.code_claude_md.
_ISOLATED_DIRNAME = ".claude-local"
_DROP_SETTINGS_KEYS = ("hooks", "enabledPlugins", "mcpServers")  # break jinja / Claude-only / MCP off by choice
_MIRROR_SUBDIRS = ("skills", "agents")
MINIMAL_CLAUDE_MD = """\
# Local model session

You are a local model backing Claude Code. Keep responses direct and focused on
the user's explicit request.

- Do NOT invoke skills, slash-commands, background tasks or memory tooling on
  your own initiative — the user drives those when needed.
- When asked to produce or edit a file, output the complete file.
- Prefer doing the requested work over describing process.
"""


def _mirror_dir(src: Path, dst: Path) -> None:
    """Mirror a config subdir into the isolated profile. Prefer a live
    symlink/junction (auto-fresh, cheap); fall back to a copy. No-op if src is
    absent or dst already exists (links stay live across launches)."""
    if not src.is_dir() or dst.exists():
        return
    try:
        os.symlink(src, dst, target_is_directory=True)
        return
    except OSError:
        pass
    if os.name == "nt":                       # symlinks need privilege; junctions don't
        try:
            import _winapi
            _winapi.CreateJunction(str(src), str(dst))
            return
        except (OSError, AttributeError):
            pass
    shutil.copytree(src, dst, dirs_exist_ok=True)  # last resort: goes stale, but works


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def build_isolated_config(home: Path, dst: Path, claude_md_src=None) -> Path:
    """Materialize a CLAUDE_CONFIG_DIR that keeps skills and agents but strips the
    hooks / Claude-only plugins / MCP and the full user CLAUDE.md that break (or
    overload) a small local model. CLAUDE.md is a minimal stub — or, if
    claude_md_src points to an existing file, that file. settings.json/CLAUDE.md
    are rewritten every launch so real config stays in sync; .claude.json is
    carried (MCP stripped) so onboarding/auth state doesn't reset. Returns dst."""
    dst.mkdir(parents=True, exist_ok=True)
    override = Path(claude_md_src) if claude_md_src else None
    if override and override.is_file():
        shutil.copyfile(override, dst / "CLAUDE.md")
    else:
        (dst / "CLAUDE.md").write_text(MINIMAL_CLAUDE_MD, encoding="utf-8")
    settings = _read_json(home / "settings.json")
    for k in _DROP_SETTINGS_KEYS:
        settings.pop(k, None)
    (dst / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    dot = _read_json(home / ".claude.json")   # oauth/onboarding state; drop MCP by choice
    if dot:
        dot.pop("mcpServers", None)
        (dst / ".claude.json").write_text(
            json.dumps(dot, ensure_ascii=False), encoding="utf-8")
    for sub in _MIRROR_SUBDIRS:
        _mirror_dir(home / sub, dst / sub)
    return dst


def code(cfg, extra: list[str] | None = None) -> int:
    """Run Claude Code (foreground, inherits stdio) against the running local
    model, under an isolated CLAUDE_CONFIG_DIR profile (see build_isolated_config)."""
    try:
        llm = _llm_cfg(cfg)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    state = load_state(cfg)
    if not state or not proc.is_running(state.get("server_pid") or -1):
        print("[mb] no local model running — run 'mb llm start <model>'",
              file=sys.stderr)
        return 1
    claude = shutil.which("claude")
    if not claude:
        print("[mb] 'claude' CLI not found — npm install -g @anthropic-ai/claude-code",
              file=sys.stderr)
        return 1
    host = llm.get("server_host", "127.0.0.1")
    port = state.get("server_port") or int(llm.get("server_port", 8080))
    env = dict(os.environ)
    env.update({
        # bare origin — Claude Code appends /v1/messages itself
        "ANTHROPIC_BASE_URL": f"http://{host}:{port}",
        "ANTHROPIC_AUTH_TOKEN": resolve_auth_token(llm, os.environ),
        "ANTHROPIC_MODEL": str(state.get("model")),
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    })
    env.pop("ANTHROPIC_API_KEY", None)
    home = Path(llm.get("claude_home") or (Path.home() / ".claude"))
    iso = Path(llm.get("code_config_dir") or (Path.home() / _ISOLATED_DIRNAME))
    build_isolated_config(home, iso, llm.get("code_claude_md"))
    env["CLAUDE_CONFIG_DIR"] = str(iso)        # isolated profile: minimal CLAUDE.md, no hooks / plugins
    args = list(extra or [])
    if args[:1] == ["--"]:                 # argparse.REMAINDER keeps the separator
        args = args[1:]
    return subprocess.run([claude, *args], env=env).returncode


# --- mb llm setup -----------------------------------------------------------

_VALUED_FLASH_ATTN = ("-fa", "--flash-attn")   # llama-server >= b9xxx: needs on|off|auto


def fix_model_args(args: list[str]) -> tuple[list[str], list[str]]:
    """Repair known argv pitfalls; returns (new_args, fixes). Currently: a bare
    -fa/--flash-attn swallows the next flag as its value — insert 'on'."""
    out: list[str] = []
    fixes: list[str] = []
    for i, a in enumerate(args):
        out.append(a)
        if a in _VALUED_FLASH_ATTN:
            nxt = args[i + 1] if i + 1 < len(args) else None
            if nxt is None or str(nxt).startswith("-"):
                out.append("on")
                fixes.append(f"{a} -> {a} on")
    return out, fixes


_WIN_CUDA_ZIP = re.compile(r"bin-win-cuda.*x64\.zip$")
_CUDA_VER = re.compile(r"cuda-?(\d+(?:\.\d+)?)")


def pick_release_assets(assets: list[dict]) -> tuple[dict, dict | None]:
    """Pick the Windows-CUDA-x64 server zip (highest CUDA version) and its
    matching cudart runtime zip, if published. Naming drifts across releases —
    on no match, raise with the available names so the pattern can be updated."""
    def cuda_ver(name: str) -> float:
        m = _CUDA_VER.search(name)
        return float(m.group(1)) if m else 0.0

    mains = [a for a in assets
             if _WIN_CUDA_ZIP.search(a["name"])
             and not a["name"].startswith("cudart-")]
    if not mains:
        names = ", ".join(a["name"] for a in assets) or "(no assets)"
        raise LLMConfigError(f"no win-cuda-x64 zip in release assets: {names}")
    main = max(mains, key=lambda a: cuda_ver(a["name"]))
    cudarts = [a for a in assets
               if a["name"].startswith("cudart-") and _WIN_CUDA_ZIP.search(a["name"])]
    cudart = next((a for a in cudarts
                   if cuda_ver(a["name"]) == cuda_ver(main["name"])),
                  cudarts[0] if cudarts else None)
    return main, cudart


def _ssl_context():
    """truststore (OS cert store) if available — this machine sits behind a
    TLS-intercepting proxy; plain certifi bundles fail on GitHub downloads."""
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return None


def fetch_json(url: str, ctx=None) -> dict:
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "singularity-mb"})
    with urllib.request.urlopen(req, timeout=30, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def download(url: str, dest: Path, ctx=None) -> None:
    """Stream url to dest via a .part file (atomic finish, partial cleanup)."""
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60, context=ctx) as resp, \
                open(tmp, "wb") as out:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r[mb] {dest.name}: {done >> 20}/{total >> 20} MiB",
                          end="", flush=True)
            if total:
                print()
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_DRIVE_ABS = re.compile(r"^[A-Za-z]:")


def _unsafe_entry(name: str) -> bool:
    """True iff an archive entry could escape `target` on extraction — POSIX
    absolute, Windows drive-absolute, or containing a '..' segment (checked
    after normalizing backslashes to forward slashes, since pathlib treats
    both as separators on Windows)."""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or _DRIVE_ABS.match(norm):
        return True
    return ".." in norm.split("/")


def unpack_zip(zip_path: Path, target: Path) -> None:
    """Extract into target, stripping a single top-level directory if present."""
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if any(_unsafe_entry(n) for n in names):
            raise LLMConfigError(f"unsafe path in archive: {zip_path.name}")
        tops = {n.split("/", 1)[0] for n in names}
        prefix = (tops.copy().pop() + "/"
                  if len(tops) == 1 and all("/" in n for n in names) else "")
        for name in names:
            rel = name[len(prefix):] if name.startswith(prefix) else name
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)


def server_bin_ok(path: Path) -> bool:
    """True iff the binary exists and `--version` exits 0 (sanity, not health)."""
    try:
        return subprocess.run([str(path), "--version"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


# --- mb llm setup -----------------------------------------------------------

DEFAULT_INSTALL_DIR = "E:/llama.cpp"       # big files never on C: (machine rule)
LLAMA_RELEASE_API = ("https://api.github.com/repos/"
                     "ggml-org/llama.cpp/releases/latest")
_SERVER_EXE = "llama-server.exe"


def _default_llm_section(install_dir: Path) -> dict:
    return {
        "server_bin": str(install_dir / _SERVER_EXE),
        "server_host": "127.0.0.1",
        "server_port": 8080,
        "server_ready_path": "/health",
        "health_timeout_s": 300,
        "models": {},
    }


def setup(cfg, dir_override: str | None = None) -> int:
    """Verify/repair the local-LLM environment. Idempotent. Exit 0 unless a
    blocking item remains (binary unavailable and not auto-installable)."""
    lines: list[tuple[str, str]] = []      # (level, message)

    def report(level: str, msg: str) -> None:
        lines.append((level, msg))
        out = sys.stderr if level == "fail" else sys.stdout
        print(f"[{level}] {msg}", file=out)

    changed = False
    section = cfg.data.get("llm")
    if not section:
        install_dir = Path(dir_override or DEFAULT_INSTALL_DIR)
        section = _default_llm_section(install_dir)
        cfg.data["llm"] = section
        changed = True
        report("fixed", "created llm config section "
                        f"(server_bin -> {section['server_bin']})")
    elif not isinstance(section, dict):
        report("fail", f'llm config section must be an object, got '
                       f"{type(section).__name__} — fix the machine config (~/.singularity.json)")
        return 1
    else:
        report("ok", "llm config section present")

    # --- llama.cpp binary --------------------------------------------------
    server_bin_raw = section.get("server_bin")
    server_bin = None
    if not server_bin_raw or not isinstance(server_bin_raw, str):
        report("fail", 'llm.server_bin not set — add e.g. "server_bin": '
                       '"E:/llama.cpp/llama-server.exe" to the machine config (~/.singularity.json)')
    else:
        server_bin = Path(server_bin_raw)
        target_dir = Path(dir_override) if dir_override else server_bin.parent
        if server_bin_ok(server_bin):
            report("ok", f"llama-server works: {server_bin}")
        elif sys.platform == "win32":
            try:
                ctx = _ssl_context()
                release = fetch_json(LLAMA_RELEASE_API, ctx)
                main, cudart = pick_release_assets(release.get("assets") or [])
                target_dir.mkdir(parents=True, exist_ok=True)
                for asset in filter(None, (main, cudart)):
                    dest = target_dir / asset["name"]
                    report("fixed", f"downloading {asset['name']}")
                    download(asset["browser_download_url"], dest, ctx)
                    unpack_zip(dest, target_dir)
                    dest.unlink(missing_ok=True)
                exes = sorted(target_dir.rglob(_SERVER_EXE))
                new_bin = exes[0] if exes else target_dir / _SERVER_EXE
                if not server_bin_ok(new_bin):
                    raise LLMConfigError(
                        f"downloaded binary failed --version: {new_bin}")
                section["server_bin"] = str(new_bin)
                changed = True
                report("fixed", f"installed llama-server -> {new_bin}")
            except Exception as exc:
                report("fail", f"llama.cpp install failed: {exc} — "
                               f"download manually into {target_dir}")
        else:
            report("fail", f"llama-server missing at {server_bin} — install "
                           "manually (auto-download is Windows-only): "
                           "https://github.com/ggml-org/llama.cpp/releases")

    # --- model args sanity + gguf presence ---------------------------------
    models = section.get("models")
    if models is not None and not isinstance(models, dict):
        report("fail", f"llm.models must be an object, got {type(models).__name__}")
        models = {}
    models = models or {}
    if not models:
        report("warn", 'model registry empty — add e.g. "models": '
                       '{"name": {"gguf": "E:/models/x.gguf", '
                       '"args": ["-ngl", "99", "-fa", "on", "--jinja"]}}')
    for name, model in models.items():
        if not isinstance(model, dict):
            report("fail", f"llm.models.{name} must be an object, got {type(model).__name__}")
            continue
        args_raw = model.get("args")
        if args_raw is not None and not isinstance(args_raw, list):
            report("fail", f"llm.models.{name}.args must be a list, got {type(args_raw).__name__}")
            continue
        gguf_raw = model.get("gguf")
        if gguf_raw is not None and not isinstance(gguf_raw, str):
            report("fail", f"llm.models.{name}.gguf must be a string, got {type(gguf_raw).__name__}")
            continue

        fixed_args, fixes = fix_model_args(list(args_raw or []))
        if fixes:
            model["args"] = fixed_args
            changed = True
            report("fixed", f"{name}: " + "; ".join(fixes))
        if not has_chat_template({"models": {name: model}}, name):
            report("warn", f"{name}: args lack --jinja/--chat-template — "
                           "tool-calling won't work")
        gguf = Path(gguf_raw or "")
        if gguf.exists():
            report("ok", f"{name}: gguf present")
        else:
            report("warn", f"{name}: gguf missing: {gguf}")

    # --- claude CLI ---------------------------------------------------------
    if shutil.which("claude"):
        report("ok", "claude CLI in PATH")
    else:
        report("warn", "claude CLI not found — "
                       "npm install -g @anthropic-ai/claude-code "
                       "(blocks 'mb llm code' only)")

    # --- isolated Claude Code profile for the local model ------------------
    # Prebuild the CLAUDE_CONFIG_DIR now so 'mb llm code' starts clean instead
    # of materializing it on first run. Idempotent (build_isolated_config).
    try:
        home = Path(section.get("claude_home") or (Path.home() / ".claude"))
        iso = Path(section.get("code_config_dir") or (Path.home() / _ISOLATED_DIRNAME))
        build_isolated_config(home, iso, section.get("code_claude_md"))
        report("ok", f"local Claude profile ready: {iso}")
    except Exception as exc:
        report("warn", f"local Claude profile not built: {exc}")

    if changed:
        cfg.save()

    counts = {lvl: sum(1 for l, _ in lines if l == lvl)
              for lvl in ("ok", "fixed", "warn", "fail")}
    summary = (f"[mb] setup: {counts['ok']} ok, {counts['fixed']} fixed, "
               f"{counts['warn']} warnings, {counts['fail']} blocking")
    print(summary, file=sys.stderr if counts["fail"] else sys.stdout)
    return 1 if counts["fail"] else 0
