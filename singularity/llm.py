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


def poll_health(url: str, timeout_s: float, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
