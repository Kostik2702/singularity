# Design — `mb llm`: local inference server supervisor

**Date:** 2026-07-08
**Status:** approved (user), pending implementation
**Owner:** user (kosnatntin.gritsuk@gmail.com)
**Component:** `singularity/llm.py` (new) + `singularity/cli.py` (subcommand wiring)

## Problem

The user wants one-command start/stop of a local LLM inference server (e.g.
`ThinkingCap-Qwen3.6-27B` GGUF, or any other model), cross-platform, delivered
as a first-class feature of the Singularity project (CLI `mb`). The end goal is
to point Claude Code (or any client) at a locally hosted model. Because Claude
Code speaks the Anthropic wire format while `llama-server` exposes an
OpenAI-compatible endpoint, a proxy/translator (router) must run alongside.
Approved scope: **one command brings up both processes** and prints a ready
`ANTHROPIC_BASE_URL`.

## Goals

- `mb llm start <model>` / `stop` / `status` / `list` / `logs` — cross-platform.
- Supervise **two** long-lived processes: `llama-server` + a router
  (translator). Both are described declaratively in config so the concrete
  router (claude-code-router / LiteLLM / other) is swappable without code change.
- Reuse the existing cross-platform process layer; add minimal new code.
- Health-gate the `started` message: do not report success until both endpoints
  answer.
- Only one model server active at a time (single-GPU VRAM); starting a new model
  stops the previous server first.

## Non-goals (YAGNI)

- No downloading of models or binaries.
- No auto-install of the router / Node / LiteLLM.
- No mutation of `~/.claude` settings or env — only **print** the export line.
  (`mb llm env` for shell-eval export may come later.)
- No multi-model concurrent serving, no load balancing, no GPU scheduling.

## Existing building blocks (reused 1:1)

- `singularity/platform/proc.py`:
  - `spawn_detached(args, log_file) -> pid` — DETACHED_PROCESS+CREATE_NO_WINDOW
    on Windows, `start_new_session=True` (setsid) on POSIX. Redirects
    stdout/stderr to a log file. (`proc.py:28`)
  - `is_running(pid) -> bool` (psutil, `tasklist`/`os.kill` fallback). (`proc.py:55`)
  - `kill(pid) -> bool` (psutil terminate, `taskkill`/`os.kill` fallback). (`proc.py:74`)
  - `read_pid(pid_file) -> int|None`. (`proc.py:94`)
- `singularity/config.py`:
  - `Config` with `.mb_dir` derived-path pattern; all runtime state lives under
    `<vault>/.mb/` for portability. New state files follow the same convention.
  - `DEFAULT_CONFIG` dict + `Config.get(key, default)` merge pattern. (`config.py:27`, `config.py:88`)
- `singularity/cli.py`: argparse subparser registration + `cmd_*` dispatch
  pattern (mirrors `cmd_daemon`, `config.py`/`cli.py:112`).

## Command surface

```
mb llm start <model>            # server -> health -> router -> health; prints base_url
mb llm stop                     # router -> server (reverse); clears state
mb llm status [--json]          # active model, both pids, ports, uptime, health
mb llm list                     # models from registry; active one marked
mb llm logs [server|router] [-n N]   # tail a log (default: server, N=40)
```

Exit codes: `0` success; `1` operational failure (bin/gguf missing, health
timeout, not running when expected); `2` reserved for argparse/config errors
(consistent with existing CLI).

## Configuration

New `"llm"` section in `<vault>/.mb/config.json`. Machine-specific paths live in
the vault config (the vault is already portable per machine; on another OS the
user edits only paths, commands stay identical). Added to `DEFAULT_CONFIG` as an
empty/comment-documented stub so `mb llm` prints actionable guidance when unset.

```json
"llm": {
  "server_bin": "E:\\llama.cpp\\llama-server.exe",
  "server_host": "127.0.0.1",
  "server_port": 8080,
  "server_ready_path": "/health",
  "router": {
    "cmd": ["ccr", "start"],
    "host": "127.0.0.1",
    "port": 3456,
    "ready_path": "/health"
  },
  "health_timeout_s": 120,
  "models": {
    "qwen27b": {
      "gguf": "E:\\models\\ThinkingCap-Qwen3.6-27B-Q4_K_M.gguf",
      "args": ["-ngl", "99", "-c", "16384", "--jinja"]
    }
  }
}
```

Notes:
- `router` may be omitted → single-process mode (server only); `start` then skips
  router steps and prints the OpenAI-compatible base URL instead of an Anthropic one.
- `--jinja` (or an explicit `--chat-template`) is required for tool-calling; on
  `start`, if none of the model `args` contain `--jinja`/`--chat-template`, warn
  (do not hard-fail — user may know better).

## State

Single JSON file `<vault>/.mb/llm.state.json`:

```json
{
  "model": "qwen27b",
  "server_pid": 12345,
  "server_port": 8080,
  "router_pid": 12346,
  "router_port": 3456,
  "started_at": "2026-07-08T14:30:00"
}
```

Logs: `<vault>/.mb/llm-server.log`, `<vault>/.mb/llm-router.log` (append mode,
same as daemon log). `started_at` is stamped by the running process (allowed;
scripts-only `Date.now` restriction does not apply to normal Python code).

## Control flow

### start `<model>`
1. Resolve config; if `llm.server_bin` unset or `models[<model>]` absent → error
   with guidance.
2. Validate `server_bin` exists and model `gguf` exists (fail fast, exit 1).
3. If `llm.state.json` present and `server_pid` alive → this is a model switch:
   run the full `stop` sequence first (free VRAM).
4. Build server argv: `[server_bin, "-m", gguf, "--host", host, "--port",
   str(port), *model.args]`. **Pure function** `build_server_argv(cfg, model)` —
   unit-tested without a real binary.
5. `spawn_detached(server_argv, llm-server.log)` → record `server_pid`.
6. Poll `http://host:port{server_ready_path}` until 200 or `health_timeout_s`.
   On timeout: kill server, clear partial state, exit 1 with a pointer to the log.
7. If `router` configured: `spawn_detached(router.cmd, llm-router.log)` →
   `router_pid`; poll router health the same way (kill both on timeout).
8. Write `llm.state.json`.
9. Print: active model, base URL, and the export line:
   `export ANTHROPIC_BASE_URL=http://<router.host>:<router.port>`
   (or the OpenAI base URL if no router).

### stop
1. Read `llm.state.json`; if absent → "not running", exit 0.
2. `proc.kill(router_pid)` if present, then `proc.kill(server_pid)` (reverse of
   start). Best-effort; a `None`/dead pid is a no-op.
3. Delete `llm.state.json`. Print "stopped".

### status
- No state file → "not running".
- Else report model, pids (+ `is_running` liveness), ports, uptime (now −
  `started_at`), and a live health probe of each endpoint. `--json` dumps the
  enriched state. If a pid is dead but state exists → flag "stale" and hint `stop`.

### list
- Enumerate `llm.models` keys with gguf path + a `(active)` marker for the one in
  state. Empty registry → guidance on adding a model to config.

### logs
- Tail last N lines of the selected log file (default server). Missing file →
  friendly message.

## New module layout

`singularity/llm.py`:
- `build_server_argv(cfg_llm, model_name) -> list[str]` — pure.
- `poll_health(url, timeout_s) -> bool` — uses stdlib `urllib` (no new dep).
- `load_state(cfg) / write_state(cfg, dict) / clear_state(cfg)` — pure-ish I/O.
- `start/stop/status/list_models/logs(cfg, ...)` — orchestration returning int
  exit codes, mirroring `cmd_daemon` style.

`singularity/cli.py`:
- Register `llm` subparser with nested actions (mirror the `note` nested-subparser
  pattern already in `_build_parser`).
- `cmd_llm(cfg, args)` dispatch → `singularity/llm.py`.

No new third-party dependency (`urllib`, `subprocess`, `psutil` already present).

## Error handling

- Missing/typo config keys → single actionable stderr line + exit 1, never a
  traceback (consistent with `[mb] ...` style elsewhere).
- Health timeout → kill whatever was started, clear partial state, point at the
  relevant `.mb/llm-*.log`.
- `stop` on already-dead pids → idempotent, exit 0.
- Windows console UTF-8 already forced globally in `cli.main`.

## Testing

- `build_server_argv` — table of (config, model) → expected argv (flags order,
  host/port injection, gguf path). No binary needed.
- state round-trip: `write_state` → `load_state` → `clear_state`.
- `poll_health` against a stdlib `http.server` stub returning 200 / 503 /
  connection-refused → true / retry-then-false / false.
- `start` happy path with `spawn_detached`/`poll_health` monkeypatched (no real
  server) → asserts order (server before router), state written, correct
  base URL printed.
- switch path: existing alive state triggers `stop` before new spawn.
- Cross-platform proc behavior is already covered by existing `proc.py` usage;
  not re-tested here.

## Router recommendation (informational)

Config is router-agnostic. Default `cmd: ["ccr", "start"]`
(claude-code-router, Node) — purpose-built for Claude Code, smoother tool-calling.
Alternative: LiteLLM proxy (pip, stays in Python, no Node) — set
`router.cmd` to the LiteLLM invocation. Either is a config edit, no code change.

## Open items / future

- `mb llm env` (print export in a `eval`-able form, or write a dotfile) — later.
- Optional auto-`ANTHROPIC_BASE_URL` wiring into a launched Claude Code — out of
  scope now (no `~/.claude` mutation by design).
