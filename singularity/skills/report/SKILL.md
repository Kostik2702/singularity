---
name: report
description: Capture an assistant mistake as a PENDING error-report note in the vault (incidents/, tag error-report) with root cause analysis. Triggers: report, mistake, "ты ошибся", "запиши ошибку", wrong answer, "зафиксируй фейл". AUTO-TRIGGER: when the user corrects the assistant twice on the same thing, offer to run this skill.
---

# report — error capture (self-learning pipeline: INPUT)

Target folder: `incidents/`. This skill only RECORDS (status PENDING); converting reports into rules is the `error-reports` skill.

## Search default

- [ ] BEFORE writing, check for similar past errors: `mb search "<error description>" --type incident --tags error-report` (also `--type rule` — maybe a rule already exists and was violated: that itself is a finding). Grep is fallback for exact strings.

## Step 1: ONE compound ask (infer first, confirm — never ask empty)

No serial diagnostic questions. The error usually just happened in this session, so:

1. **Infer every answer from the conversation FIRST**: what was claimed (own prior message), what was actually true (user's correction), error type, which verification was skipped, task context, target note.
2. **Present the reconstruction** (2-4 lines: claimed vs reality + skipped verification) so the user confirms instead of re-typing.
3. **Ask ONE compound AskUserQuestion** only for still-uncertain dimensions, each pre-filled with the inferred value as first option marked `(inferred)`:
   - **Error type** — see taxonomy below
   - **Context** — current task (MB-N / project) / recent incident / general conversation
   - **What was missed** — code not read / logs not checked / docs not consulted / config not verified / state not checked / recent changes not reviewed (multiSelect)
4. Dimensions unambiguous from context → don't ask; state them `(inferred)` and let the user object.

## Step 1.5: Primary-source verification (MANDATORY when the report blames an agent/subagent run)

**A secondhand error report — a retelling, or your own memory of a past session — is a HYPOTHESIS, not a finding.** Recording its mechanism without checking the primary source is how a confident-but-false root cause gets propagated. Before writing Root Cause:

- [ ] **Codebase claims → check directly, in seconds.** "File was UTF-16 / a stub / had X" → `file <f>`, `head -c2 <f> | xxd -p` (BOM `fffe`/`feff`/`efbbbf`?), `grep -c <marker> <f>`. "Function/field doesn't exist" → `mb search` / Grep.
- [ ] **Agent-behaviour claims → read the agent's OWN transcript, never the narrative.** Subagents: `~/.claude/projects/<session>/subagents/agent-*.jsonl`; main session: `~/.claude/projects/<session>/<id>.jsonl`. The **decisive artifact is the tool-call histogram** (works in Git Bash):
  ```bash
  jq -rc 'select(.message.content)|.message.content[]?|select(.type=="tool_use")|.name' "$F" | sort | uniq -c
  ```
  **Empty histogram = the agent executed nothing** → every "shell hang / heredoc / silent write / API error" claim about that run is physically impossible; the real failure is tool-call hallucination (markup emitted as text). Useful extras: all Bash commands verbatim (`...select(.name=="Bash")|.input.command`), last 12 entries (where the run actually ended).
- [ ] **Ground truth for "the agent changed files"** = `git status` / `git diff --stat` in the target repo. Zero diff under a confident "done" = hallucinated tool calls, full stop.
- [ ] **Confidence gate** (via `confidence-calibration` skill): a mechanism not confirmed by transcript or codebase stays **≤70%, labelled "secondhand — unverified"**. Only the primary source promotes it, and only the `error-reports` skill promotes the note to LEARNED.

## Step 2: timestamp

```bash
date "+%Y-%m-%d %H:%M:%S"
```

## Step 3: write the PENDING note

Create `{{VAULT}}/incidents/Error - <short title>.md` (or append a `## #error-report` block to an existing related incident/learnings note — dedup per Step 0 search). Note contract (frontmatter mandatory):

```markdown
---
type: incident
title: "Error - <short title>"
tags: [error-report, <topic tags>]
created: YYYY-MM-DD
source: "<task ID / session context>"
---

# Error - <short title>

**Date**: YYYY-MM-DD HH:MM:SS
**Error Type**: <from taxonomy>
**Context**: <task / incident / conversation>
**Status**: PENDING

## What happened
[Raw facts only — artifacts: tool histogram, git diff, file bytes, error text, the wrong claim verbatim. NOT a story; prose is not evidence.]

## What was expected
[The correct behaviour/answer, per the user's correction or the verified source.]

## Context
[Task, files, environment; wikilinks to related vault notes, e.g. [[MB-16]].]

## Diagnostic questions
[Which verification was skipped? What would have caught it earlier? Inferred mechanism (hypothesis + confidence; secondhand stays ≤70% until Step 1.5). Root cause layered: proximate / driver / deepest; mark verified vs unverified layers.]

## Corrective actions
**Immediate**: [...]
**Preventive (candidate rule)**: [...]
- [ ] GATE — would this rule have caught EXACTLY this failure? If not — it's theater, drop it here already.
- [ ] Defang — if the failure was format/injection-driven, escape or inert-fence any tool-call markup / payload quoted in this note so it can't re-trigger downstream.

## Lesson
[One sentence.]
```

- [ ] After saving, tell the user: "PENDING report saved; run `error reports` to convert it into a rule."

## Status values

- `PENDING` — recorded, not yet converted to a rule
- `LEARNED (rule added to [location] on YYYY-MM-DD)` — set only by the `error-reports` skill

## Error types

| Type | Description |
|------|-------------|
| `wrong-code` | Suggested code that doesn't work |
| `wrong-analysis` | Incorrect diagnosis |
| `hallucination` | Referenced non-existent things |
| `missed-context` | Didn't consider relevant info (incl. vault memory not searched) |
| `overconfidence` | High confidence without evidence |
| `stale-info` | Info was outdated |
| `partial-view` | Missed other usages/impacts |
| `wrong-assumption` | Assumed without verifying |
| `tool-call-hallucination` | Agent emitted tool-call markup as text — 0 tools executed; verify via the histogram (Step 1.5) |
| `secondhand-unverified` | Mechanism recorded without checking the primary source — capped ≤70% until verified |

## Few-shot

1. **Everything inferable**: user says "report" right after correcting a broken suggestion in task MB-12 → infer wrong-code + claimed/reality + "code not read"; one compound ask, all `(inferred)`; write note to `incidents/`.
2. **Agent-failure claim**: "the subagent hung on a heredoc and lost all work" → treat as HYPOTHESIS; transcript histogram comes back empty → heredoc-hang physically impossible; record CORRECTED mechanism as `tool-call-hallucination`, note the initial version was `secondhand-unverified`.
