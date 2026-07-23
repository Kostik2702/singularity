# GRK-00 — Grok Build CLI contract fixtures

Redacted, reproducible evidence for the Grok Build CLI integration (GRK-01..07).
Every claim below is **OBSERVED** on a real isolated run unless explicitly marked
ASSUMED. If a future Grok version drifts, re-capture with
`tools/grok_contract_probe.py` and let `tests/test_grok_contract.py` flag what changed.

## Tested environment

| Item | Value |
|---|---|
| CLI version | `grok 0.2.111 (94172f2aa4)` — from `grok --version` and `grok version` (identical output, parseable as `grok <semver> (<hash>)`) |
| OS | Windows 11 Pro 10.0.26200 |
| Date | 2026-07-23 |
| Auth | **None.** All authenticated turns went through a local fake OpenAI-compatible endpoint (`[model.probe-model] base_url = "http://127.0.0.1:8399/v1"` in the isolated `config.toml`). No real credentials, no xAI network traffic. |
| Isolation | `HOME`, `USERPROFILE`, `GROK_HOME` pointed at a throwaway tree; sentinel file proved the fake home was used. See "Home resolution" for the Windows caveat. |
| Capture harness | `tools/grok_contract_probe.py` (fake endpoint + hook dumper + scenario runner) |

`MIN_GROK_VERSION = "0.2.111"` — the only version these contracts are proven against.

## Capability matrix

| Capability | Status | Evidence |
|---|---|---|
| `grok inspect --json` discovery report (instructions/skills/hooks/compat cells) | OBSERVED works, offline | `inspect_discovery.json` |
| `GROK_HOME` overrides the whole native `~/.grok` tree (config, skills, hooks, sessions, rules, AGENTS.md) | OBSERVED | sessions/fixtures written under isolated grokhome |
| `GROK_HOME` unset → falls back to **env** `HOME`/`USERPROFILE` `\.grok` | OBSERVED | inspect picked `home\.grok\Agents.md` |
| Claude-compat **skills** scanner uses env `HOME` | OBSERVED | `probe-claude-only` discovered from fake home |
| Claude-compat **agents (CLAUDE.md), hooks (settings.json), plugins** scanners use the **WinAPI profile dir** and IGNORE env `HOME`/`USERPROFILE` | OBSERVED (Windows) | real `~\.claude\Claude.md`, real hooks and plugins appeared despite redirected env |
| Skill dedup: native `GROK_HOME/skills/<name>` **wins** over Claude-compat same-name skill | OBSERVED | dupe skill resolved to grokhome variant |
| Junction-linked skill dirs (`mklink /J`) under `GROK_HOME/skills` are discovered | OBSERVED | `probe-linked` in inspect |
| `[compat.claude] * = false` in config.toml → instructions marked `disabled: true` | OBSERVED | inspect run C |
| `[compat.claude] hooks = false` → hook entries **still listed without a disabled marker** in inspect | OBSERVED (runtime execution under disabled cell NOT verified) | inspect run C |
| `GROK_CLAUDE_*_ENABLED=false` env vars | **DID NOT WORK** on 0.2.111 (cells stayed `source=default`) | inspect run B |
| Hooks execute in **headless** mode (`-p`) | OBSERVED | debug log + dumps |
| Hook stdin payload: **camelCase** envelope, event values snake_case (`session_start`, `stop`, `pre_tool_use`, `post_tool_use`) | OBSERVED | `hook_*.json` |
| Stop payload: `transcriptPath`, `promptId`, `reason`, `stopHookActive`, `lastAssistantMessage`, `backgroundTasks[]`, `sessionCrons[]` | OBSERVED | `hook_stop.json` |
| Stop hook `{"decision":"block","reason":...}` **blocks the stop and feeds reason to the model** (headless too) | OBSERVED — reason text appeared in next model request; `num_turns` grew to 2; second Stop fire had `stopHookActive: true` | T2 run |
| SessionStart / UserPromptSubmit stdout context injection | **NOT SUPPORTED** — raw stdout ignored | sentinel absent from model requests |
| SessionStart / UserPromptSubmit Claude-style `hookSpecificOutput.additionalContext` | **NOT SUPPORTED** on 0.2.111 | sentinel absent from model requests |
| Hook env vars `GROK_HOOK_EVENT/NAME`, `GROK_SESSION_ID`, `GROK_WORKSPACE_ROOT`, `CLAUDE_PROJECT_DIR` injected | OBSERVED | `hook_env_vars.json` |
| Headless `--output-format json` stdout byte-clean (stderr empty, exit 0) | OBSERVED | `headless_json_result.json` |
| Headless `streaming-json` NDJSON, last event `type:"end"` | OBSERVED | `stream_json_events.jsonl` |
| Headless `plain` = bare text | OBSERVED | `PROBE-REPLY-OK` |
| Sessions: `~/.grok/sessions/<urlencoded-cwd>/<uuidv7>/` with `summary.json`, `chat_history.jsonl`, `updates.jsonl` (ACP `method`/`params` rows), `signals.json`, `events.jsonl`, `rewind_points.jsonl`, `system_prompt.txt`, `prompt_context.json` | OBSERVED | `session_summary_toplevel.json` |
| Subagent markers: parent `subagents/<child-id>/{meta.json,output.json}`; child is a normal session with `"session_kind": "subagent"` in summary.json | OBSERVED | `subagent_meta.json`, `session_summary_subagent.json` |
| `chat_history.jsonl` row types: `system` / `user` / `assistant` / `tool_result`; real user prompts carry `prompt_index`, injected context carries `synthetic_reason` (`project_instructions`, `system_reminder`) | OBSERVED | `chat_history_tooluse.jsonl` |
| Rules delivery: AGENTS.md + rules dirs injected as a synthetic user message wrapped in `<system-reminder>`; **UTF-8 BOM leaks into model context as `п»ї`** | OBSERVED | write managed files BOM-less |
| Hook JSON files with UTF-8 BOM are **silently skipped** | OBSERVED | BOM'd probe.json → 0 hooks; BOM-less → loaded |
| SKILL.md frontmatter with BOM: frontmatter ignored (falls back to dirname + first body paragraph) | OBSERVED | dupe-skill description showed body text |

## Windows hook command quoting (PowerShell!)

Hook `command` strings are executed via **PowerShell** on Windows (empirical: `$0` → empty,
`%COMSPEC%` literal, `echo a b` one-arg-per-line, single/double-quote semantics match PS).

| Form | Result |
|---|---|
| `"C:\quoted\python.exe" "script.py" ARG` (Claude settings.json style) | **FAILS** (~207 ms, PS parse error, fail-open) — this is why the existing Singularity Claude hooks fail inside Grok on Windows (exit 1, no effect) |
| `D:/fwd/slash/python.exe script.py ARG` (unquoted) | works |
| `D:\back\slash\python.exe script.py ARG` (unquoted) | works |
| `& "D:\quoted\python.exe" "script.py" ARG` (call operator) | works |
| `cmd /c "..."` and `.cmd` wrapper | works |
| `$env:VAR` in the command string | **CORRUPTS the command** — `$VAR`/`${VAR}` expansion happens at hook **load time** against the env/`env` map; undefined names expand to empty (`$env` itself gets eaten). Do not reference env vars in the command string; read them inside the script (they ARE set on the child process). |
| `%VAR%` | never expanded (literal) |

Arg quoting: backslashes are preserved inside single and double quotes; `"sp ace"` is one arg.
See `hooks_windows_example.json` for the two safe shapes.

## Duplicate-source precedence decision (Milestone A gate)

- **Skills**: native `GROK_HOME/skills` beats `~/.claude/skills` on name collision → shipping
  native links is safe and is the effective single source. Claude-compat skills stay as a
  fallback for users without native install.
- **Rules/instructions**: `GROK_HOME/AGENTS.md` and real `~/.claude/CLAUDE.md` are **both**
  loaded (additive, no dedup) → the managed native `AGENTS.md` block must NOT duplicate
  CLAUDE.md content, only add Grok-specific deltas, or the model sees the text twice.
- **Hooks**: `~/.claude/settings.json` hooks and `GROK_HOME/hooks/*.json` are **both loaded
  and both executed** (observed 10 hooks from 2 sources in one session) → a native Grok hook
  that duplicates a Claude hook would double-fire. Today the Claude-style commands fail on
  Windows (quoting), acting as an accidental guard; a native hook adapter MUST either make
  the events disjoint or keep commands Windows-PS-safe AND idempotent.

## GO / NO-GO decisions (tested CLI: grok 0.2.111, fixtures: this directory)

- **GRK-04 dynamic SessionStart recall: NO-GO.** No context-injection channel exists
  (stdout and `additionalContext` both ignored on SessionStart/UserPromptSubmit).
  Fallback: static recall instructions inside the managed `GROK_HOME/AGENTS.md` block
  (delivered to every session as `project_instructions`), agent executes `mb recall` itself.
- **GRK-04 capture reminder (stop-nudge): GO — IMPLEMENTED** as
  `singularity/hooks/grok_stop_capture.py`, registered by `install/register_grok.py`
  into `<grok-home>/hooks/singularity.json`. Real-CLI smoke (0.2.111, fake endpoint):
  the block reason reached the model exactly once; the once-per-session marker
  (`capture-grok-<sid>`) suppressed repeats; baseline-vs-hooked headless JSON had
  identical schema keys and clean stderr — the only difference is the documented,
  bounded extra round (`num_turns` 1→2, `text` concatenates the two turns), the same
  semantics as Claude's block gate. Edit success/failure classified via the fixed
  tool_result prefixes in `chat_history_edit_results.jsonl` ('Tool `x` failed:' /
  'Failed to parse arguments'); shell success requires the 'exit: 0' echo.
- **GRK-05 reflect parser: GO.** `chat_history.jsonl` is a stable typed JSONL
  (`prompt_index` marks real user prompts; `tool_calls`/`tool_result` carry tool usage;
  `model_id` per assistant row); subagent sessions are excludable via
  `summary.json.session_kind == "subagent"`; parent-side aggregates live in
  `subagents/<id>/meta.json`. `signals.json` already provides counters (turnCount,
  toolCallCount, toolsUsed, modelsUsed, contextTokensUsed…) — prefer it for metrics before
  parsing transcripts.

## GRK-06 test matrix

- **Unit/contract tests** (default CI, 3 OS × py3.10–3.12): every `test_grok_*`
  file runs fully isolated (env `GROK_HOME` + patched `Path.home`; a leaked
  developer `GROK_HOME` is explicitly `delenv`-ed in fixtures). No network, no
  auth, no real profile mutation.
- **grok-e2e** (`pytest -m grok_e2e`, opt-in `workflow_dispatch` job in
  `.github/workflows/ci.yml`): installs the official stable CLI
  (`curl -fsSL https://x.ai/cli/install.sh | bash` on POSIX,
  `irm https://x.ai/cli/install.ps1 | iex` on Windows), then drives
  version-probe → register → real `grok inspect --json` discovery → doctor →
  exact-owned uninstall in an isolated `GROK_HOME`, plus a spaces-in-path root.
  **No xAI credentials anywhere** — sessions are never started; authenticated
  headless checks are deliberately absent from CI (they run only via
  `tools/grok_contract_probe.py` against a local fake endpoint).
- **Windows-only scenarios**: junction removal preserves the engine target;
  the registered hook command uses the call-operator shape (`& "..."`).
- **Real Windows smoke** (2026-07-23, grok 0.2.111, this machine): 5/5
  `grok_e2e` tests passed against the real binary; platform capability
  downgrade: none beyond the documented compat-scanner env-isolation limit.

## Known gaps / residual risks

1. Real-credential smoke (OAuth/`XAI_API_KEY`) not run — deliberately (no-network rule).
   Fake endpoint speaks `/v1/chat/completions`; the OAuth path may inject different
   system prompts or cost fields.
2. Runtime effect of `[compat.claude] hooks = false` unverified (inspect still lists them).
3. On Windows, Claude-compat agents/hooks/plugins scanners read the **real** user profile —
   contract tests on Windows cannot fully isolate them; CI isolation is Linux-only (env HOME
   honored there — ASSUMED from scanner using env on the skills path, needs a Linux run).
4. Interactive TUI hook parity spot-checked only via headless; `/hooks` modal not scripted.
5. `updates.jsonl` params schema not pinned (ACP `session/update` + `_x.ai/session/update`);
   GRK-05 should treat `chat_history.jsonl` + `signals.json` as its inputs.
6. MCP leak: sessions auto-connect real `~/.claude.json` MCP servers even in the isolated
   env (WinAPI profile). Diagnostics (doctor) must never start sessions, only `grok inspect`.
7. `SessionEnd` hook never fired in headless `-p` runs (0 dumps across all scenarios) —
   headless consumers must not depend on it; TUI behavior unverified.
