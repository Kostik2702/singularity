---
name: mb-llm-delegate
description: Delegate a generation/edit task to the running local model (Singularity mb llm) as the executor while you orchestrate. Use when the user says "delegate it to the local model", "let the local model do it", "orchestrate the local model", "delegate to local model", or wants to offload code/text generation to the self-hosted model to save orchestrator tokens.
---

# mb-llm-delegate — orchestrator ↔ local executor

The local model (`mb llm start <model>`, endpoint `127.0.0.1:PORT/v1/messages`,
Anthropic-compatible) is the **executor**. You are the **orchestrator/controller**.

## Why this way, and not a full Claude Code on the local model

A small model collapses in an agentic loop: it confuses and re-enters tool calls
(`TaskOutput(mb-search)` was observed looping forever). So it gets **single
prompts with NO tool use** — it can carry generation, while tools, files and
review stay with the orchestrator. No jinja crashes and no loops.

## Division of roles (strict)

- **The executor (local model):** writes ALL the code — both the first version and
  the fixes. It returns the complete file as text.
- **The orchestrator (you):** formulate the task, hand over the current file,
  **write its output to disk** (mechanical transcription — the model has no file
  tools), and review the result. Found a bug — **do not fix it yourself**:
  formulate the correction and hand it back to the executor, which returns a
  corrected complete file; you write it and check again. Iterate until clean.

## Usage

```bash
SK="$HOME/.claude/skills/mb-llm-delegate/delegate.py"

# generating a new file
python "$SK" --task "Create ..." --outfile D:/path/app.html

# an edit or a new feature: hand over the current file plus the instruction, get a complete new one
python "$SK" --infile D:/path/app.html \
  --task "Add X. Return the COMPLETE updated HTML file in full." \
  --outfile D:/path/app.html

# fixing a bug you found (you find it, it fixes it)
python "$SK" --infile D:/path/app.html \
  --task "Bug: <description>. Fix it and return the COMPLETE file." \
  --outfile D:/path/app.html
```

Flags: `--task` **or** `--taskfile <path>` (mutually exclusive, one is required),
`--system` (your own system prompt; the default is "output ONLY complete file, no
fences"), `--max-tokens` (default 6000), `--print` (echo to stdout),
`--keep-partial` (on truncation keep the partial in `<outfile>.partial` instead of
discarding it); without `--outfile` the output goes to stdout.

⚠️ **PowerShell:** double quotes inside task text passed inline through `--task`
(even in a here-string `@'...'@`) break python's argument parsing (`exit 2
unrecognized arguments`) and produce cp1251 mojibake. For tasks containing quotes
or brackets, write the text into a UTF-8 file and pass `--taskfile`:

```powershell
Set-Content -Path task.txt -Encoding utf8 -Value '...text with "quotes" and (brackets)...'
python $SK --taskfile task.txt --infile D:/path/app.html --outfile D:/path/app.html --max-tokens 12000
```

`--outfile` is written ONLY atomically and ONLY on a complete non-empty response
(exit 0). On `stop_reason=max_tokens` (exit 2) or empty output (exit 1) the
existing `--outfile` is NOT touched — raise `--max-tokens` and retry.

## Quality control (mandatory on every iteration)

- [ ] Read the written file; do not take it on trust.
- [ ] Syntax: `node --check` for JS, the compiler or linter for your language.
- [ ] Logic and resources: leaks (creating DOM nodes or subscriptions in a loop),
      off-by-one, truncation at `max_tokens`, transforms that drifted, and so on.
- [ ] A bug becomes an instruction to the executor (see the example above), NOT a
      hand edit.

## Requirements

- The model is up: `mb llm status --json` → `server_alive: true`. The script takes
  the endpoint, port and model from there itself (fallback `127.0.0.1:8080`).
- Ask the model to **always return the whole file** (never a diff) — that keeps the
  write atomic and removes any merging.

Related: [[SINGULARITY-mb-llm]] (the local server supervisor, the jinja template fix, 128k context).
