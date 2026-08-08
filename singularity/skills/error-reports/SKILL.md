---
name: error-reports
description: Review PENDING error-report notes in the vault, formulate preventive rules, embed each rule at its execution point, mark reports LEARNED. Triggers: error reports, process reports, work through the mistakes, PENDING reports.
---

# error-reports — errors into rules (self-learning pipeline: OUTPUT)

Input: PENDING notes created by the `report` skill.

## Step 1: find all PENDING reports

- [ ] Grep `Status.*PENDING` in `{{VAULT}}/incidents/` (decisive) + `mb search "error report" --tags error-report` (catches strays in learnings/ etc).

## Step 2: display as numbered list

One line each: Date, Error Type, Context, Lesson. Wait for the user to pick a number.

## Step 3: formulate the preventive rule + placement

Analyze the root cause, then propose rule option(s), each with the **EXECUTION POINT** where it belongs — the artifact that will be loaded exactly at the moment the error could repeat, NEVER a general lessons list:

- a specific skill in `C:\Users\Admin\.claude\skills\<name>\SKILL.md` — error maps to a workflow that skill drives (e.g. overconfidence → `confidence-calibration`, missed vault memory → `mb-search`)
- a rule note in vault `rules/` — cross-cutting technical rule; **filename shape: `rules/Rule - <what the rule says>.md`** (frontmatter `type: rule`; the template's sections: the rule itself as checkboxes / why — which failure it catches, GATE result / origin — task, date, LEARNED line)
- a `CLAUDE.md` section — permanent behavioral rule active in every session
- an agent file in `.claude\agents\` — error is specific to one subagent's behaviour

Present options with reasoning; user approves the location.

## Step 4: GATES before embedding

- [ ] **Anti-theater GATE: "Would this rule have caught EXACTLY this failure?"** Replay the incident mentally with the rule in place. If it would not have fired — drop the rule, look for a different prevention (different check, tooling change, hook), or honestly record: "not catchable by a rule."
- [ ] **Rule-consistency**: `mb search "<rule essence>" --type rule` — the new rule must not contradict or duplicate existing rules; if overlap, extend the existing rule note instead of creating a new one.

## Step 5: apply

1. Add the rule to the approved location(s) (rule notes get frontmatter per the format sample; wikilink back to the incident note).
2. Edit the PENDING note: `Status: LEARNED (rule added to [location] on YYYY-MM-DD)` — get the date via `date "+%Y-%m-%d"`.
3. One-line entry in `changelog/YYYY-MM.md` (vault contract for significant changes).
