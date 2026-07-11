# Gemini CLI contract fixtures (GCLI-00 spike)

Redacted fixtures captured from a real, isolated run of the official
`@google/gemini-cli` on Windows. They pin the hook / transcript / headless
contract that the Singularity Gemini integration (GCLI-01…07) depends on.

## Version matrix (minimum tested)

| Component | Version |
|---|---|
| `@google/gemini-cli` | **0.50.0** (stable) |
| Node.js | v20.18.2 |
| npm | 11.17.0 |
| OS | Windows 11 Pro 26200 |

`0.50.0` provides `gemini hooks` (migrate), `gemini skills` (list/enable/disable/install/link/uninstall),
`-o text|json|stream-json`, `--fake-responses[-non-strict]` (hidden test flags).
The task note's documented lower bound was 0.26.0; 0.50.0 is the pinned floor for these fixtures.

## Hook base payload (every event)

`session_id`, `transcript_path`, `cwd`, `hook_event_name`, `timestamp` — plus per-event fields.
**There is NO interactive/headless discriminator field in any payload.** (Confirmed empirically.)

## Lifecycle events confirmed firing (headless `-p`)

SessionStart (`source`), BeforeAgent (`prompt`), AfterAgent (`prompt`,`prompt_response`,`stop_hook_active`),
SessionEnd (`reason`). All four fire in headless mode.

## Auto-capture go/no-go verdict

**`decision:"deny"` (retry) = NO-GO for unconditional default-on.**
In headless `-o json` a deny forces an extra model turn AND mutates the output:
the `response` field becomes the concatenation of both turns and a top-level
`warnings[]` array is added (see `headless_json_deny_mutation.json`). Because the
payload carries no headless discriminator, a hook cannot gate the retry off.

**`systemMessage` (passive reminder) = SUPPORTED / safe default.**
A `systemMessage`-only AfterAgent response leaves headless `-o json` and `-o text`
output byte-identical to the clean baseline — no retry, no extra model turn, no
`warnings` (see `headless_json_clean.json`). This is the recommended primitive for
GCLI-04: remind-to-capture via `systemMessage`, not `decision:deny`.

Exactly-once retry state machine (if deny is ever used, e.g. opt-in): first pass has
`stop_hook_active:false`, the retry pass has `stop_hook_active:true` — gate on it.

## Transcript format (`~/.gemini/tmp/<project>/chats/session-*.jsonl`)

Event-sourced JSONL: line 1 = session meta `{sessionId, projectHash, startTime, kind}`;
subsequent lines are either full message objects or `{"$set":{...}}` partial updates.
Message `type` is `"user"` (`content:[{text}]`) or `"gemini"` (`content:"...", thoughts, tokens, model`).
**Read as UTF-8** — non-ASCII (Cyrillic, box-drawing) corrupts under the default Windows codepage.

### Tool-use representation (captured — `transcript_tooluse.jsonl`, `stream_json_events.jsonl`)

Transcript: a tool call lives on a `type:"gemini"` message in a **`toolCalls[]`** array
(`{id, name, args, result:[{functionResponse:{response:{output}}}], status, resultDisplay, displayName}`);
the result is ALSO echoed as a following `type:"user"` message with `content:[{functionResponse}]`.
**There is NO top-level `type:"tool_use"` line** — GCLI-04's parser must read `toolCalls`, not a
`"tool_use"` string (the task note warned about exactly this).

Headless `-o stream-json` event stream: `init` → `message`(user) → `tool_use{tool_name,tool_id,parameters}`
→ `tool_result{tool_id,status,output?,error?}` → `message`(assistant, `delta:true`) → `result{status,stats}`.

Tool hooks `BeforeTool{tool_name,tool_input}` / `AfterTool{...,tool_response{llmContent,returnDisplay}}`
fire in headless yolo on a *valid* tool call (they do NOT fire if the tool fails param validation first).

## Native overlap: Gemini "Auto Memory"

Gemini 0.50.0 ships experimental `experimental.autoMemory` (off by default): it mines
*past* idle sessions (≥3h, ≥10 user msgs) into a `/memory inbox` of `.patch`/`SKILL.md`
candidates in `~/.gemini`. This does NOT replace Singularity capture (different store =
vault, different trigger = current-session reminder). Document coexistence; do not rely on it.

## Headless schema (real, 0.50.0)

`-o json` → `{ session_id, response, stats{ models, tools, files }, warnings?[], error? }`
(the task note's `{response, stats, error?}` was missing `session_id` and `warnings`).
Exit codes: 0 success; 1 error/API; 42 input error; 53 turn-limit (per docs).
