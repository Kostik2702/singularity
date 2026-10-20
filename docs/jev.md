# Jev - the TypeSafe decision API

Jev answers a yes/no, a choice or a score about a text, with a probability. The engine uses it for three hints, all off until `mb jev on`: the size of an incoming task (UserPromptSubmit), the executor tier for a spawn brief (`mb jev pick`), and a rerank of `mb search`. Jev never writes anything and never edits a tool call.

API: `POST https://api.typesafe.ai/v1/systemone`, `Authorization: Bearer <key>`, model pinned to `jev-1.13.0`. Cost is computed in code from `usage.input_tokens` at $0.042 per million (one constant in `singularity/jev/client.py`). There is no balance endpoint: set `jev_budget_usd` to what was topped up and the balance is that minus the journal total.

## Setup

```
pbpaste | mb jev key set          # macOS; Windows: Get-Clipboard | mb jev key set
                                  # Linux: xclip -o -selection clipboard | mb jev key set  (Wayland: wl-paste)
mb jev on
mb config set jev_budget_usd 10   # optional
mb jev status
```

The key is read from stdin only, stored in the OS keyring under the fixed name `jev_api_key`, and never appears in argv, logs or error messages. `MB_JEV_API_KEY` overrides the stored key (tests, broken-key drill).

## Config

| Key | Default | Meaning |
|---|---|---|
| `jev_enabled` | absent (off) | master switch; `mb jev off` removes it |
| `jev_budget_usd` | null | what was topped up |
| `jev_search` | true | rerank `mb search` when Jev is on |

## Guarantees

- The router hook (`singularity/hooks/jev_router.py`, a second UserPromptSubmit entry, `timeout: 5`) exits 0 silently on every failure path, never exits 2, never emits a decision. A watchdog hard-exits 0 at 4.5 s; Jev gets 3.5 s with a deadline that bounds its retries.
- Below confidence 0.6 Jev does not decide: nothing is injected and the journal says `decided: self`.
- Every outgoing text passes `redact()`. Search sends title, section and a 600-character excerpt of wiki notes only; notes classified personal (frontmatter `project: personal` or under `personal_roots`) are never sent.
- Retries: 429 and 529 only, pauses 1, 2, 4, 8, 16 s (or `retry-after`), never past the caller's deadline.

## Decisions made on the owner's behalf (ASSUMED, lead, 2026-10-20)

- **Follow-ups are judged first.** A message the noul reads as a follow-up (p >= 0.6) inherits the session's previous route and is `decided: jev`. With no previous route it is `decided: self` and nothing is injected - what it continues is unknown, and "yes, do the whole plan" reads as a tiny task. Everything else goes by the size question and its confidence (< 0.6 = `self`). The size is `floor(score + 0.5)` clamped to 0..3.
- **Large tasks** get `sonnet·high executors + opus·high review; plan first` (team-lead §3: opus is for checks).
- **Outbound personal check is stricter than the engine's.** A note under any `personal_roots` path is never sent, even when its frontmatter names a project; the engine's own classification (explicit project wins) is untouched elsewhere.
- **Harness-injected turns are skipped** (no call, no journal line): prompts that START with `<task-notification`, `<system-reminder`, `[SYSTEM NOTIFICATION`, `<local-command`, `<command-name`, `<command-message`, `Caveat: The messages below`, `Stop hook feedback:` or `Hook additional context:`. Slash commands are skipped only when they look like one (`/word` followed by a space or the end); a path such as `/Users/me/x.py fails` is a prompt.

## Safety of the call

- Every failure of the HTTP layer (refused, reset, garbage status line, short body, TLS, timeout) is a `JevError`; rerank, pick and the router also catch anything else. The result is the engine's own order, `pick: decide yourself (...)`, or silence, plus a journal line.
- The deadline is total: the request runs in a daemon thread that is abandoned at the deadline (hook, pick, search 3.5 s; `ask` 10 s), so a slow drip or a resolver that never answers cannot stretch it.
- Redirects are never followed (a 3xx is `http:<code>`), so the key cannot travel to another host.
- `MB_JEV_ENDPOINT` is honoured only when `MB_JEV_API_KEY` is set too (the stored key never goes to an overridden URL), and plain http only to 127.0.0.1/localhost.
- The hook is registered through an inline launcher (`python -c "...subprocess.call(...); sys.exit(0)"`): a missing or broken `jev_router.py` would otherwise exit 2 and erase every prompt. The other UserPromptSubmit hook is registered as a bare script and has that exposure; it is out of scope here.
- `mb search --top N`: only the first 30 hits are reranked; a larger N keeps the rest after them in the engine's order. With Jev on, the daemon is asked for more candidates, so with hybrid search the fused pool can differ slightly from a plain search - `--no-jev` gives the plain one.

## Journal

`<vault>/.mb/machines/<host>/jev/log.jsonl`, one JSON object per line (`mb jev status --json` prints the path). Fields: `ts, session, kind (router|pick|search|ask|probe), head, chars, size, followup, confidence, decided (jev|self), route, cost, ms, model, request_id, error ("<kind>:<status>" or null), test`. Past 5 MiB the file rotates to `log.1.jsonl` under a lock. The generation that rotation overwrites is first summed into `totals.json` (`{"cost": ...}`) next to the journal, and `mb jev status` (`total_cost`, hence `balance_usd`) is both generations plus that carry, so the all-time cost never resets. `error` is `"<kind>:<status>"`, with `null` spelled out when there is no status (`"timeout:null"`). A call whose answer cannot be read, or that times out after the vendor processed it, is logged at cost 0: the balance is slightly optimistic by those.

## Drill

```
echo '{"prompt":"...","session_id":"t"}' | MB_JEV_TEST=1 .venv/bin/python singularity/hooks/jev_router.py
MB_JEV_API_KEY=wrong ...   # expect: no output, exit 0
```

Test seam: `MB_JEV_ENDPOINT` points the client at a local server; the tests never call the vendor.
