# Design — `mb llm code` + `mb llm setup`: Claude Code on a local model, auto-setup

**Date:** 2026-07-09
**Status:** approved (user), pending implementation
**Owner:** user (kosnatntin.gritsuk@gmail.com)
**Component:** `singularity/llm.py` (extend) + `singularity/cli.py` (wiring)
**Supersedes:** the router premise of `2026-07-08-mb-llm-server-supervisor-design.md`
(§Problem, §Router recommendation). The `llm.router` config contract and spawn
code stay as-is (future multi-model CCR), but a router is **no longer required**.

## Key verified facts (2026-07-09)

- `llama-server` (build b9936 installed) natively exposes the **Anthropic
  Messages API**: `POST /v1/messages` + `POST /v1/messages/count_tokens`,
  streaming SSE, tool-use. Verified live against the running server: HTTP 200,
  correct Anthropic response shape. Requires `--jinja` (already in model args).
  Source: [HF blog "Anthropic Messages API in llama.cpp"](https://huggingface.co/blog/ggml-org/anthropic-messages-api-in-llamacpp),
  [llama-server README](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md).
- Claude Code connects to it directly: `ANTHROPIC_BASE_URL=http://127.0.0.1:8080`
  (bare origin, **no `/v1`** — the client appends paths itself).
- Auth: Claude Code needs a non-empty token env; the local server (no
  `--api-key`) ignores it — verified: unauthenticated curl returns 200.
  Set exactly ONE of `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_API_KEY` (both → conflict).
- `ANTHROPIC_SMALL_FAST_MODEL` is deprecated (replacement:
  `ANTHROPIC_DEFAULT_HAIKU_MODEL`); irrelevant here — llama-server serves the
  single loaded model regardless of the requested model id, so no haiku-tier
  override is needed.
- New llama-server changed `-fa` from a boolean flag to a valued option
  `[on|off|auto]`; a bare `-fa` swallows the next argv token (bug hit 2026-07-09,
  fixed in config by hand; `setup` must auto-fix it — see below).

## Goals

1. `mb llm code [-- <claude args>]` — one command: run Claude Code against the
   running local model (env prepared, foreground, interactive).
2. `mb llm setup [--dir PATH]` — fully automatic environment install/repair:
   config section, llama.cpp binary (auto-download), model args sanity,
   `claude` CLI presence. Idempotent; safe to re-run.

## Non-goals (YAGNI)

- No GGUF model downloads from HF (largest scope, TLS-MITM friction — later).
- No claude-code-router / LiteLLM install (native endpoint removed the need;
  `llm.router` config support stays dormant for a future multi-model phase).
- No `ANTHROPIC_DEFAULT_*_MODEL` overrides (single local model answers all tiers).
- No keyring for the auth token while the server runs without `--api-key`.
  **TODO:** if `--api-key` is ever added to server args, move the token to
  `platform/secrets.py` (keyring) — config.json lives in the (syncable) vault.
- No auto-install of the `claude` CLI itself (print the npm instruction).
- POSIX auto-download: out of scope (Windows CUDA build only; on other OS
  `setup` prints manual instructions for the binary step, the rest works).

## Command 1 — `mb llm code`

`code(cfg, extra: list[str]) -> int` in `singularity/llm.py`:

1. `load_state(cfg)`; if absent or `server_pid` dead →
   `[mb] no local model running — run 'mb llm start <model>'`, exit 1.
2. `base = f"http://{llm.server_host|127.0.0.1}:{state['server_port']}"`.
3. Env = copy of `os.environ` overlaid with:
   - `ANTHROPIC_BASE_URL` = base
   - `ANTHROPIC_AUTH_TOKEN` = token chain (below)
   - `ANTHROPIC_MODEL` = `state["model"]`
   - `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` = `1` (guards against 400s on
     beta fields the local server doesn't know)
4. `claude = shutil.which("claude")`; missing → exit 1 + npm hint.
5. `subprocess.run([claude, *extra], env=env)` — **foreground**, inherits stdio
   (interactive session; NOT `spawn_detached`). Return its returncode.

**Token chain** (first non-empty wins):
1. `os.environ["ANTHROPIC_AUTH_TOKEN"]` — user override, never clobbered;
2. config `llm.auth_token` — optional new key;
3. literal `"local"` — dummy; server ignores auth (verified).

`mb llm start` output gains one hint line: `Run Claude Code on it: mb llm code`.

CLI: `code` subparser under `llm` with `argparse.REMAINDER` positional
(`claude_args`) so `mb llm code -- --resume` passes through.

## Command 2 — `mb llm setup`

`setup(cfg, dir_override: str | None) -> int`. Steps, each reported as a
✓/✗/fixed line; exit 0 if all green (warnings allowed), 1 if a blocking item
remains.

### 2.1 Config section
- `llm` missing → create it with defaults: `server_host 127.0.0.1`,
  `server_port 8080`, `server_ready_path /health`, `health_timeout_s 300`,
  empty `models {}`, and `server_bin` pointing into the install dir (2.2).
- Write-back is atomic (tmp + replace), preserving unrelated keys.

### 2.2 llama.cpp binary (auto-download, Windows)
- Target dir: `--dir` > parent of existing `server_bin` > `E:/llama.cpp`
  (big files never on C: — machine rule).
- If `server_bin` exists AND `llama-server.exe --version` exits 0 → ✓ done.
- Else download from GitHub `ggml-org/llama.cpp` **latest release**:
  - `GET https://api.github.com/repos/ggml-org/llama.cpp/releases/latest`
    (stdlib urllib; JSON).
  - Asset selection — pure function `pick_release_assets(assets) -> (main, cudart|None)`:
    main = name matches `bin-win-cuda.*x64\.zip` (highest CUDA version if
    several); cudart = matching `cudart-.*win-cuda.*x64\.zip` if present.
    No match → error listing available asset names (naming drifts between releases).
  - Download both zips to the target dir with progress `log` lines
    (sizes are 100–500 MB), unpack (`zipfile`, flatten if single top dir),
    then verify `llama-server.exe --version` → 0.
  - **TLS-MITM machine quirk:** build the SSL context via `truststore` if
    importable (matches HF workaround in project memory), else default context.
- Update `server_bin` in config to the extracted exe path.
- POSIX: print manual build/download instructions, mark step ✗ (blocking).

### 2.3 Model args sanity (auto-fix, the `-fa` class of bugs)
Pure function `fix_model_args(args: list[str]) -> tuple[list[str], list[str]]`
(new args, list of applied fixes):
- bare `-fa`/`--flash-attn` (next token starts with `-` or is last) → insert `"on"`;
- `--jinja` and `--chat-template` both absent → warning (not a fix; user may
  intend no tool-calling).
Applied to every model in the registry; config written back only when changed.

### 2.4 Model files
- Each `models[*].gguf` checked for existence → ✓ / warning with the path
  (no download; `qwen27b` is currently missing on disk, e.g.).
- Empty registry → warning with a config snippet example.

### 2.5 `claude` CLI
- `shutil.which("claude")` → ✓ / instruction
  `npm install -g @anthropic-ai/claude-code` (Node v20 present on machine).
  Non-blocking for `setup` (blocks only `code`).

### Summary block
`[mb] setup: N ok, M fixed, K warnings, J blocking` + per-line details above.

## Config schema additions

```json
"llm": {
  "auth_token": "local",          // optional; token chain step 2
  ...existing keys unchanged...
}
```

## Error handling

- Every failure: one actionable `[mb] ...` stderr line, no tracebacks (house style).
- Download interrupted/HTTP error → remove partial zip, report URL + status.
- GitHub API rate-limit (403) → report and suggest retry/manual download URL.
- `setup` never deletes an existing working binary; new download goes to the
  same dir only after version-check failure or absence.

## Testing

Unit (no network, no binaries):
- token chain: env set / config set / neither → expected value; env wins.
- `code()`: no state → 1; dead pid → 1; happy path (mock `which` +
  `subprocess.run`) → env contains the exact 4 overlay keys, argv passthrough.
- `fix_model_args`: table — `["-fa","--jinja"]→["-fa","on","--jinja"]`;
  `["-fa","on"]` unchanged; `["-fa"]` (last) → `["-fa","on"]`; no `-fa` unchanged.
- `pick_release_assets`: fake asset lists — single match, multiple CUDA
  versions (prefers highest), cudart pairing, no match → error.
- config write-back: atomic, preserves unrelated keys, idempotent second run.
- unpack: small zip fixture → flatten behavior.

Manual E2E:
- `mb llm setup` on the current machine (binary exists → all ✓, `-fa` already
  fixed → no-op, qwen27b gguf → warning).
- `mb llm code` → interactive Claude Code; a prompt visibly hits
  `.mb/llm-server.log`; `/status` inside shows the local base URL.
- Rename `llama-server.exe` → `setup` performs a real download+unpack+verify
  (one-time, then rename back or keep).

## Module/layout notes

- All new code in the existing `singularity/llm.py` (module stays < ~450 lines;
  split into `llm/` package only if a later phase grows it further).
- `cli.py`: extend the existing `llm` subparser (cli.py:68) + `cmd_llm`
  dispatch (cli.py:216) — same pattern as start/stop/status.
- New stdlib imports only (`shutil`, `subprocess`, `zipfile`, `ssl`);
  `truststore` used opportunistically (already a project-environment tool),
  never a hard dependency.
