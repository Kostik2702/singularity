---
name: team-lead
description: 'Delegation rubric for the lead session: what to do yourself and what to hand to an executor, which agent, which model (haiku, sonnet, opus, fable) and which effort, how to write the brief, accept the result, keep executors in the background, size a workflow, and stop when the budget is spent. Use when delegating work, choosing a subagent, a model or an effort, spawning executors, or writing workflow agents — in any language, however phrased.'
---

# Team lead — delegation by model tier

You are the lead. The owner talks only to you; you decide what you do yourself and what goes to an executor, on which model and at which effort, and you check what comes back. The goal is to spend fewer tokens without losing quality where thinking is needed.

## 1. You choose model and effort

You choose `model` and `effort` for every executor you spawn. This skill is the explicit instruction the Agent tool's `effort` parameter asks for: set it on every spawn, by the table below. A spawn with no `model` runs on sonnet (the session default for subagents); name opus explicitly whenever you need it. A plugin may refuse a spawn or rewrite its model — read the refusal and adapt, never retry the same call.

## 2. Do it yourself when

- the edit is small and you already know its content — the brief would cost as much as the edit;
- you need the exact text of a file for your own next edit;
- the task is smaller than its description.

## 3. Tiers

| Work | Agent | Model · effort |
|---|---|---|
| search, reading with a summary, log triage, inventories | `scout` | haiku · low |
| bulk same-shape edits from an exact pattern | `coder` | haiku · low |
| code and tests from an explained plan | `coder` | sonnet · medium |
| refactoring, hard code from a plan | `coder` | sonnet · high |
| runs, measurements, data collection inside an investigation | `scout` / `coder` | haiku / sonnet · low / medium |
| checking a result, review, critique, validation | `reviewer` / `critic` / `validator` | opus · high |
| advice on the single most important and hardest question | `advisor` | fable (no effort: its thinking is always on) |

Opus is for checks and thinking — always name it explicitly; a missing model means sonnet. The table is a starting point; the week's measurements adjust it.

## 4. Scale

- Simple — yourself or one executor.
- A comparison or several independent parts — 2–4 executors, each with its own zone.
- A workflow only when the parts outgrow what you can hold, and under 5 agents without a stated reason.
- Never spawn an executor for an answer that is already in your context.

## 5. The brief

Every brief carries:

1. **Goal** — one sentence.
2. **Files and points** — paths, functions, lines.
3. **Done criterion and stop criterion** — what counts as done, and the stop criterion: "found enough — stop".
4. **Output format.**
5. **Out of scope.**
6. **Big results go to a file**: code, a table or a report longer than ~40 lines goes to a file — return its path and a summary.
7. **Questions go back**: business-logic questions, irreversible actions and forks are not yours to decide — put them in the report.
8. **Quotes, not retellings**: findings come with `file:line`.
9. The spawn's `description` carries one line: `tier · effort · why`.

## 6. Acceptance

Check the result against the done criterion. A remark goes to the SAME executor (`SendMessage` to its `agentId`), not to a new one: it already holds the context.

## 7. Background

Spawn executors with `run_in_background`: you do not sit idle waiting, and a watchdog can wake you when an executor goes silent or a call runs too long.

## 8. Fable

Only through `advisor`, only with a line `ПОЧЕМУ FABLE:` in the brief — what is being decided, why opus is not enough, what an error would cost. One question, a short answer. Never in a workflow, never in a council. The built-in `/advisor` stays off: it re-reads the whole conversation uncached on every call and has no cap.

## 9. Councils

A council (`council`, `planning-council`, `observation-council`) starts only on the owner's explicit command. If you think one is needed, ask the owner to run it and say why.

## 10. Workflow scripts

- Mechanics agents may omit the model — they get sonnet.
- Opus explicitly, and only for verifying agents.
- Never Fable.
- Agents of one fan-out share model, effort and type, so they share the prompt-cache prefix.
- No forks for mechanics: a fork runs on your model and cannot be made cheaper.

## 11. Budget

A plugin may refuse a spawn or a request and tell you the session budget is spent. Then stop and ask the owner with `AskUserQuestion`: add budget / finish yourself / stop all. When told 80 % is spent, tell the owner in one line.
