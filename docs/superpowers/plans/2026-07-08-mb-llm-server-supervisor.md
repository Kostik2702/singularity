# `mb llm` Local Inference Server Supervisor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a cross-platform `mb llm start/stop/status/list/logs` command that supervises a local `llama-server` plus an optional router (Anthropic↔OpenAI translator) so a locally hosted GGUF model can back Claude Code with one command.

**Architecture:** A new pure-ish module `singularity/llm.py` builds server argv from a config-driven model registry, spawns processes via the existing `singularity/platform/proc.py` cross-platform layer, health-gates readiness over stdlib `urllib`, and persists a single `.mb/llm.state.json`. `singularity/cli.py` gains an `llm` subparser dispatching to that module. No new third-party dependency.

**Tech Stack:** Python ≥3.10, stdlib (`urllib`, `subprocess`, `json`, `time`, `datetime`, `pathlib`), `psutil` (already a dep, used indirectly through `proc.py`), pytest.

## Global Constraints

- Python ≥3.10, `from __future__ import annotations` at top of new modules (matches repo style).
- No new third-party dependency — stdlib + existing `proc.py`/`psutil` only.
- All runtime state under `<vault>/.mb/` (portability convention; use `cfg.mb_dir`).
- User-facing errors print a single `[mb] ...` line to stderr and return exit code `1` — never a traceback. Argparse/config-load errors keep exit `2`.
- Only one model server active at a time; `start <model>` stops any running server first.
- Reuse `proc.spawn_detached / kill / is_running / read_pid` verbatim — do not reimplement process control.
- Windows console UTF-8 is already forced in `cli.main`; do not re-handle encoding.

---

### Task 1: argv builder, config accessor & path validation

**Files:**
- Create: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Config` (from `singularity.config`) — uses `cfg.get("llm")` and `cfg.mb_dir`.
- Produces:
  - `class LLMConfigError(Exception)`
  - `_llm_cfg(cfg) -> dict` — returns the `llm` config section or raises `LLMConfigError`.
  - `build_server_argv(llm: dict, model_name: str) -> list[str]` — pure; raises `LLMConfigError`.
  - `validate_paths(llm: dict, model_name: str) -> None` — raises `LLMConfigError` if `server_bin` or model `gguf` missing on disk.
  - `has_chat_template(llm: dict, model_name: str) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_llm.py
"""Unit tests for mb llm — no real server/binary required."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity import llm
from singularity.config import Config


def _llm(tmp_path, extra=None):
    gguf = tmp_path / "model.gguf"
    server = tmp_path / "llama-server"
    cfg = {
        "server_bin": str(server),
        "server_host": "127.0.0.1",
        "server_port": 8080,
        "models": {
            "qwen": {"gguf": str(gguf), "args": ["-ngl", "99", "--jinja"]},
            "plain": {"gguf": str(gguf), "args": ["-ngl", "10"]},
        },
    }
    if extra:
        cfg.update(extra)
    return cfg, server, gguf


def test_build_server_argv_injects_bin_gguf_host_port_and_args(tmp_path):
    cfg, server, gguf = _llm(tmp_path)
    argv = llm.build_server_argv(cfg, "qwen")
    assert argv == [str(server), "-m", str(gguf),
                    "--host", "127.0.0.1", "--port", "8080",
                    "-ngl", "99", "--jinja"]


def test_build_server_argv_unknown_model_raises(tmp_path):
    cfg, _, _ = _llm(tmp_path)
    with pytest.raises(llm.LLMConfigError):
        llm.build_server_argv(cfg, "nope")


def test_build_server_argv_missing_server_bin_raises(tmp_path):
    cfg, _, _ = _llm(tmp_path)
    del cfg["server_bin"]
    with pytest.raises(llm.LLMConfigError):
        llm.build_server_argv(cfg, "qwen")


def test_validate_paths_missing_files_raise_then_pass(tmp_path):
    cfg, server, gguf = _llm(tmp_path)
    with pytest.raises(llm.LLMConfigError):
        llm.validate_paths(cfg, "qwen")           # neither file exists yet
    server.write_text("x"); gguf.write_text("x")
    llm.validate_paths(cfg, "qwen")               # now both exist -> no raise


def test_has_chat_template(tmp_path):
    cfg, _, _ = _llm(tmp_path)
    assert llm.has_chat_template(cfg, "qwen") is True
    assert llm.has_chat_template(cfg, "plain") is False


def test_llm_cfg_missing_section_raises(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    with pytest.raises(llm.LLMConfigError):
        llm._llm_cfg(cfg)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'singularity.llm'`

- [ ] **Step 3: Create `singularity/llm.py` with the builder, accessor and validation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
cd /d/Singularity
git add singularity/llm.py tests/test_llm.py
git commit -m "feat(llm): server argv builder, config accessor, path validation"
```

---

### Task 2: state round-trip (load/write/clear)

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `build_server_argv` env (none directly); uses `cfg.mb_dir`.
- Produces:
  - `state_path(cfg) -> Path` → `<vault>/.mb/llm.state.json`
  - `load_state(cfg) -> dict | None` (None if absent/corrupt)
  - `write_state(cfg, state: dict) -> None`
  - `clear_state(cfg) -> None`
  - `_log(cfg, name: str) -> Path` → `<vault>/.mb/llm-<name>.log`

- [ ] **Step 1: Write the failing tests (append to `tests/test_llm.py`)**

```python
def test_state_round_trip(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    assert llm.load_state(cfg) is None
    llm.write_state(cfg, {"model": "qwen", "server_pid": 111})
    assert llm.state_path(cfg) == tmp_path / ".mb" / "llm.state.json"
    assert llm.load_state(cfg)["server_pid"] == 111
    llm.clear_state(cfg)
    assert llm.load_state(cfg) is None
    llm.clear_state(cfg)  # idempotent, no raise


def test_load_state_corrupt_returns_none(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    p = llm.state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert llm.load_state(cfg) is None


def test_log_path(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    assert llm._log(cfg, "server") == tmp_path / ".mb" / "llm-server.log"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k state -v`
Expected: FAIL — `AttributeError: module 'singularity.llm' has no attribute 'state_path'`

- [ ] **Step 3: Append state helpers to `singularity/llm.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k "state or log_path" -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /d/Singularity
git add singularity/llm.py tests/test_llm.py
git commit -m "feat(llm): .mb/llm.state.json round-trip + log path helpers"
```

---

### Task 3: health polling

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `poll_health(url: str, timeout_s: float, interval: float = 1.0) -> bool` — returns True on first 2xx, False after `timeout_s`. Retries on connection refused / 5xx.

- [ ] **Step 1: Write the failing tests (append to `tests/test_llm.py`)**

```python
import http.server
import threading


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.__class__.status)
        self.end_headers()

    def log_message(self, *a):  # silence
        pass


def _serve(status):
    _Handler.status = status
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def test_poll_health_true_on_200():
    srv, port = _serve(200)
    try:
        assert llm.poll_health(f"http://127.0.0.1:{port}/health", 2, interval=0.05) is True
    finally:
        srv.shutdown()


def test_poll_health_false_on_persistent_503():
    srv, port = _serve(503)
    try:
        assert llm.poll_health(f"http://127.0.0.1:{port}/health", 0.3, interval=0.05) is False
    finally:
        srv.shutdown()


def test_poll_health_false_on_connection_refused():
    # nothing listening on this port
    assert llm.poll_health("http://127.0.0.1:9/health", 0.3, interval=0.05) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k poll_health -v`
Expected: FAIL — `AttributeError: module 'singularity.llm' has no attribute 'poll_health'`

- [ ] **Step 3: Append `poll_health` to `singularity/llm.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k poll_health -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd /d/Singularity
git add singularity/llm.py tests/test_llm.py
git commit -m "feat(llm): stdlib health polling with retry/timeout"
```

---

### Task 4: `start` orchestration (server → router, health-gated, switch)

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `_llm_cfg`, `build_server_argv`, `validate_paths`, `has_chat_template`, `poll_health`, `load_state`, `write_state`, `clear_state`, `_log`, `proc.spawn_detached`, `proc.is_running`, `proc.kill`.
- Produces: `start(cfg, model_name: str) -> int`
  - Order: validate → (switch: stop if a live server exists) → spawn server → poll `/health` → if `router` configured, spawn router → poll → write state → print `export ANTHROPIC_BASE_URL=...` (router) or the OpenAI base URL (no router). On any health timeout: kill what was started, `clear_state`, return 1.

- [ ] **Step 1: Write the failing tests (append to `tests/test_llm.py`)**

```python
def _ready_llm(tmp_path, with_router=False):
    cfg, server, gguf = _llm(tmp_path)
    server.write_text("x"); gguf.write_text("x")   # pass validate_paths
    if with_router:
        cfg["router"] = {"cmd": ["ccr", "start"], "host": "127.0.0.1",
                         "port": 3456, "ready_path": "/health"}
    return Config(vault=tmp_path, data={"llm": cfg})


def test_start_server_only_writes_state_and_prints_openai_url(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    spawned = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 0
    state = llm.load_state(cfg)
    assert state["model"] == "qwen" and state["server_pid"] == 4242
    assert state["router_pid"] is None
    out = capsys.readouterr().out
    assert "127.0.0.1:8080" in out
    assert len(spawned) == 1                       # server only


def test_start_with_router_spawns_both_in_order_and_prints_base_url(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path, with_router=True)
    order = []
    monkeypatch.setattr(llm.proc, "spawn_detached",
                        lambda argv, log: order.append(argv[0]) or (5000 + len(order)))
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 0
    # server binary spawned before router command
    assert order[0].endswith("llama-server") and order[1] == "ccr"
    state = llm.load_state(cfg)
    assert state["router_pid"] == 5002
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:3456" in capsys.readouterr().out


def test_start_server_health_timeout_kills_and_clears(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    killed = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 7777)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 1
    assert 7777 in killed
    assert llm.load_state(cfg) is None


def test_start_switch_stops_existing_first(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    llm.write_state(cfg, {"model": "old", "server_pid": 999,
                          "router_pid": None, "server_port": 8080})
    calls = {"kill": [], "spawn": 0}
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: pid == 999)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: calls["kill"].append(pid))
    def _spawn(argv, log):
        calls["spawn"] += 1
        return 4242
    monkeypatch.setattr(llm.proc, "spawn_detached", _spawn)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    assert llm.start(cfg, "qwen") == 0
    assert 999 in calls["kill"]                     # old server killed before new spawn
    assert llm.load_state(cfg)["server_pid"] == 4242


def test_start_missing_gguf_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    Path(cfg.get("llm")["models"]["qwen"]["gguf"]).unlink()   # remove gguf
    assert llm.start(cfg, "qwen") == 1
    assert "gguf not found" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k start -v`
Expected: FAIL — `AttributeError: module 'singularity.llm' has no attribute 'start'`

- [ ] **Step 3: Append `start` to `singularity/llm.py`**

```python
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
    if existing and proc.is_running(existing.get("server_pid") or -1):
        print(f"[mb] stopping active model '{existing.get('model')}' first")
        stop(cfg)

    timeout = float(llm.get("health_timeout_s", 120))
    host = llm.get("server_host", "127.0.0.1")
    port = int(llm.get("server_port", 8080))
    ready = llm.get("server_ready_path", "/health")

    server_pid = proc.spawn_detached(argv, _log(cfg, "server"))
    if not poll_health(f"http://{host}:{port}{ready}", timeout):
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
        router_pid = proc.spawn_detached([str(a) for a in router["cmd"]],
                                         _log(cfg, "router"))
        if not poll_health(f"http://{r_host}:{r_port}{r_ready}", timeout):
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
```

Note: `start` references `stop` — it is defined in Task 5. Within a module, function bodies resolve names at call time, so the forward reference is fine; the switch test in this task monkeypatches `proc.kill`/`is_running`, and `stop` (added in Task 5) only calls those + `clear_state`. If executing tasks strictly in order, add a minimal `stop` stub now and let Task 5 replace it; the provided Task 5 body is identical in effect.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k start -v`
Expected: PASS (5 passed). If `test_start_switch_stops_existing_first` errors with `NameError: stop`, proceed to Task 5 (adds `stop`) then re-run.

- [ ] **Step 5: Commit**

```bash
cd /d/Singularity
git add singularity/llm.py tests/test_llm.py
git commit -m "feat(llm): start orchestration — server->router, health-gated, VRAM switch"
```

---

### Task 5: `stop`, `status`, `list_models`, `logs`

**Files:**
- Modify: `singularity/llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `stop(cfg) -> int` — kills router then server (reverse order), clears state; "not running" if no state.
  - `status(cfg, as_json: bool = False) -> int` — prints model/pids/ports/uptime + live `is_running` liveness; flags stale state.
  - `list_models(cfg) -> int` — lists registry models, marks active.
  - `logs(cfg, which: str = "server", n: int = 40) -> int` — tail a log file.

- [ ] **Step 1: Write the failing tests (append to `tests/test_llm.py`)**

```python
def test_stop_not_running(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={})
    assert llm.stop(cfg) == 0
    assert "not running" in capsys.readouterr().out


def test_stop_kills_router_then_server_and_clears(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={})
    llm.write_state(cfg, {"model": "q", "server_pid": 11, "router_pid": 22})
    order = []
    monkeypatch.setattr(llm.proc, "kill", lambda pid: order.append(pid))
    assert llm.stop(cfg) == 0
    assert order == [22, 11]                        # router before server
    assert llm.load_state(cfg) is None
    assert "stopped" in capsys.readouterr().out


def test_status_reports_and_flags_dead(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={})
    llm.write_state(cfg, {"model": "q", "server_pid": 11, "server_port": 8080,
                          "router_pid": None, "started_at": "2026-07-08T14:00:00"})
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.status(cfg) == 0
    out = capsys.readouterr().out
    assert "q" in out and "DEAD" in out


def test_status_not_running(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={})
    assert llm.status(cfg) == 0
    assert "not running" in capsys.readouterr().out


def test_list_models_marks_active(tmp_path, capsys):
    inner, server, gguf = _llm(tmp_path)
    cfg = Config(vault=tmp_path, data={"llm": inner})
    llm.write_state(cfg, {"model": "qwen"})
    assert llm.list_models(cfg) == 0
    out = capsys.readouterr().out
    assert "qwen" in out and "(active)" in out and "plain" in out


def test_logs_tail(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={})
    log = llm._log(cfg, "server")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("l1\nl2\nl3\n", encoding="utf-8")
    assert llm.logs(cfg, "server", n=2) == 0
    out = capsys.readouterr().out
    assert "l2" in out and "l3" in out and "l1" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k "stop or status or list or logs_tail" -v`
Expected: FAIL — `AttributeError: module 'singularity.llm' has no attribute 'stop'` (or, if a stub was added in Task 4, assertion failures)

- [ ] **Step 3: Append the four commands to `singularity/llm.py`**

```python
def stop(cfg) -> int:
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
```

If Task 4 added a temporary `stop` stub, delete it before pasting this real `stop`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -v`
Expected: PASS (all tests, including Task 4's switch test now that `stop` exists)

- [ ] **Step 5: Commit**

```bash
cd /d/Singularity
git add singularity/llm.py tests/test_llm.py
git commit -m "feat(llm): stop/status/list/logs commands"
```

---

### Task 6: CLI wiring (`mb llm ...` subparser + dispatch)

**Files:**
- Modify: `singularity/cli.py` — add subparser in `_build_parser` (after the `note` parser block, ~`cli.py:66`), add `cmd_llm`, wire in `main` (~`cli.py:277`).
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `singularity.llm.{start,stop,status,list_models,logs}`.
- Produces: `cmd_llm(cfg, args) -> int`; `mb llm start <model> | stop | status [--json] | list | logs [server|router] [-n N]`.

- [ ] **Step 1: Write the failing test (append to `tests/test_llm.py`)**

```python
from singularity import cli


def test_cli_llm_status_dispatches(tmp_path, monkeypatch, capsys):
    # point the CLI at an isolated vault and stub config loading
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    tmp_path.joinpath(".mb").mkdir(parents=True, exist_ok=True)
    rc = cli.main(["llm", "status"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_cli_llm_start_parses_model_arg(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    seen = {}
    monkeypatch.setattr("singularity.llm.start",
                        lambda c, model: seen.setdefault("model", model) or 0)
    assert cli.main(["llm", "start", "qwen27b"]) == 0
    assert seen["model"] == "qwen27b"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k cli_llm -v`
Expected: FAIL — argparse exits with code 2 (`invalid choice: 'llm'`)

- [ ] **Step 3: Add the subparser in `_build_parser` (insert after the `note` block, before `learn`)**

```python
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
```

- [ ] **Step 4: Add `cmd_llm` (place next to the other `cmd_*` functions)**

```python
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
    return 2
```

- [ ] **Step 5: Wire into `main` (add alongside the other dispatch branches)**

```python
    if args.command == "llm":
        return cmd_llm(cfg, args)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest tests/test_llm.py -k cli_llm -v`
Expected: PASS (2 passed)

- [ ] **Step 7: Full suite + commit**

Run: `cd /d/Singularity && .venv/Scripts/python.exe -m pytest -q`
Expected: PASS (whole repo green)

```bash
cd /d/Singularity
git add singularity/cli.py tests/test_llm.py
git commit -m "feat(llm): wire mb llm subcommand into CLI"
```

---

### Task 7: docs — README usage + config example

**Files:**
- Modify: `README.md` (add an `mb llm` section)

**Interfaces:** none (docs only).

- [ ] **Step 1: Add an `mb llm` section to `README.md`**

Document: purpose (one command → local model behind Claude Code), the `llm`
config block (copy the example from the design spec), the five subcommands,
the two-process model (server + optional router), and the router note
(claude-code-router vs LiteLLM, config-swappable). Include the exact
`export ANTHROPIC_BASE_URL=...` follow-up step and that only paths change
across machines.

- [ ] **Step 2: Commit**

```bash
cd /d/Singularity
git add README.md
git commit -m "docs(llm): mb llm usage + config example"
```

---

## Self-Review

**Spec coverage:**
- Command surface (start/stop/status/list/logs) → Tasks 4–6. ✔
- Config `llm` section + registry → consumed in Tasks 1,4,5; documented Task 7. ✔
- State `.mb/llm.state.json` + logs → Task 2. ✔
- Reuse `proc.py` → Tasks 4,5 (spawn_detached/kill/is_running). ✔
- Health-gated start, kill-on-timeout → Task 4. ✔
- Single active model / switch stops previous → Task 4 (`test_start_switch_stops_existing_first`). ✔
- Router optional (single-process fallback prints OpenAI URL) → Task 4 (`test_start_server_only...`). ✔
- `--jinja`/chat-template warning (non-fatal) → Task 1 (`has_chat_template`) + Task 4 warning. ✔
- Error style `[mb] ...` + exit 1 → Tasks 1,4,5. ✔
- No new dependency → stdlib only throughout. ✔
- start/stop order (server→router; router→server) → Tasks 4,5 tests assert order. ✔

**Placeholder scan:** No TBD/TODO; every code step shows complete code; test bodies are concrete. The one forward reference (`start`→`stop`) is called out with an executable mitigation. ✔

**Type consistency:** State keys (`model`, `server_pid`, `server_port`, `router_pid`, `router_port`, `started_at`) identical across write (Task 4) and read (Tasks 2,5). Function names stable: `build_server_argv`, `validate_paths`, `has_chat_template`, `poll_health`, `load_state`/`write_state`/`clear_state`, `state_path`, `_log`, `start`/`stop`/`status`/`list_models`/`logs`, `cmd_llm`. ✔

**Note on `start`↔`stop` ordering:** Task 4 introduces a call to `stop` defined in Task 5. Mitigation is documented inline (temporary stub, or run Task 5's switch assertion after Task 5). Not a blocking gap.
