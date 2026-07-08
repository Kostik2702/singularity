"""mb llm — supervise a local llama-server (+ optional router) for Claude Code.

State lives in <vault>/.mb/llm.state.json; process control is delegated to
singularity.platform.proc (cross-platform detached spawn/kill).
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from .platform import proc


class LLMConfigError(Exception):
    """Missing/invalid [llm] config section or a referenced path is absent."""


def _llm_cfg(cfg) -> dict:
    data = cfg.get("llm") or {}
    if not data:
        raise LLMConfigError(
            'no [llm] config — add an "llm" section to .mb/config.json '
            "(server_bin + models)")
    return data


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
    port = int(llm.get("server_port", 8080))
    args = model.get("args") or []
    return [str(server_bin), "-m", str(gguf),
            "--host", str(host), "--port", str(port), *[str(a) for a in args]]


def validate_paths(llm: dict, model_name: str) -> None:
    server_bin = Path(llm["server_bin"])
    if not server_bin.exists():
        raise LLMConfigError(f"server_bin not found: {server_bin}")
    gguf = Path(llm["models"][model_name]["gguf"])
    if not gguf.exists():
        raise LLMConfigError(f"gguf not found: {gguf}")


def has_chat_template(llm: dict, model_name: str) -> bool:
    args = llm["models"][model_name].get("args") or []
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

    # switch: free VRAM by stopping any live server first
    existing = load_state(cfg)
    if existing and (proc.is_running(existing.get("server_pid") or -1)
                      or proc.is_running(existing.get("router_pid") or -1)):
        print(f"[mb] stopping active model '{existing.get('model')}' first")
        stop(cfg)

    timeout = float(llm.get("health_timeout_s", 120))
    host = llm.get("server_host", "127.0.0.1")
    port = int(llm.get("server_port", 8080))
    ready = llm.get("server_ready_path", "/health")

    try:
        server_pid = proc.spawn_detached(argv, _log(cfg, "server"))
    except Exception as exc:
        print(f"[mb] failed to start server: {exc}", file=sys.stderr)
        clear_state(cfg)
        return 1

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

        if not poll_health(f"http://{r_host}:{r_port}{r_ready}", timeout, pid=router_pid):
            proc.kill(router_pid)
            proc.kill(server_pid)
            clear_state(cfg)
            print(f"[mb] router not healthy in {timeout:g}s — see {_log(cfg, 'router')}",
                  file=sys.stderr)
            return 1
        state["router_pid"] = router_pid
        state["router_port"] = r_port

    write_state(cfg, state)
    print(f"[mb] started '{model_name}'")
    if router:
        print(f"export ANTHROPIC_BASE_URL=http://{router.get('host', '127.0.0.1')}:"
              f"{int(router.get('port', 3456))}")
    else:
        print(f"OpenAI-compatible endpoint: http://{host}:{port}/v1")
    return 0


def status(cfg, as_json: bool = False) -> int:
    state = load_state(cfg)
    if not state:
        print("not running")
        return 0
    server_alive = proc.is_running(state.get("server_pid") or -1)
    router_alive = (proc.is_running(state["router_pid"])
                    if state.get("router_pid") else None)
    if as_json:
        print(json.dumps({**state, "server_alive": server_alive,
                          "router_alive": router_alive},
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


def list_models(cfg) -> int:
    try:
        llm = _llm_cfg(cfg)
    except LLMConfigError as exc:
        print(f"[mb] {exc}", file=sys.stderr)
        return 1
    models = llm.get("models") or {}
    if not models:
        print("[mb] no models in config — add to llm.models in .mb/config.json")
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
