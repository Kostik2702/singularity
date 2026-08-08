---
name: grill
description: Adversarial self-interrogation gate before plans/designs/complex analysis - expose hidden assumptions, scope gaps, missing data, wrong framing. Self-resolves from codebase/memory first. Triggers: grill, grill me, grill auto, self-check before a plan or design.
allowed-tools: Read, Grep, Glob, Bash, AskUserQuestion, Skill
---

# Grill Skill

Adversarial self-interrogation before you plan, design, or run a deep analysis: *do I actually know X, or am I assuming it?* Catch a wrong framing now - cheaply - instead of building a beautiful plan of the wrong thing. Reading the task and logs makes you *feel* informed; the grill tests whether you actually are.

## Two Modes

- **Normal (`grill me`) - DEFAULT.** Surviving questions go to the USER. A distinct party grilling beats self-grilling - avoids the echo chamber of a model validating its own output.
- **Auto (`grill auto`).** Self-grill; only genuinely stuck questions escalate. Cheaper but weaker - so in auto, HIGH-STAKES unknowns escalate to the user, never self-answered on shaky ground.

Same engine and self-resolve pre-pass; only where survivors default differs.

## Step 0: Mode Select (ask FIRST unless trigger is explicit)

- [ ] Invoked as pre-check before a plan/design/complex analysis → ONE AskUserQuestion: **Auto (Recommended - I answer, escalate stuck) / Normal (you answer) / Skip grill**.
- [ ] Bare `grill` → ask Normal or Auto. `grill me` → Normal, no ask. `grill auto` → Auto, no ask.
- [ ] **Skip** → record a `[➖]` deferral in the active task note; gate opens.

## Step 1: Candidate Questions - Internal Lenses (thinking only, no tools)

Lenses are thinking prompts, NEVER a printed checklist. A lens yields a candidate ONLY when the answer is genuinely unknown AND task-specific.

- [ ] **Assumptions** - treated as true without evidence? (config/flag state, env, what's affected, data shape)
- [ ] **Terminology** - does each domain term mean what I think HERE? ("expired" = TTL or status enum?)
- [ ] **Scope & boundaries** - what's IN vs OUT? What did the task NOT ask for that I'm about to build?
- [ ] **Missing data** - what single fact would change the approach? Do I have it?
- [ ] **Failure framing** - what would make this whole framing WRONG? Likeliest alternative?
- [ ] **Who-decided** - every choice shaping the work belongs to the user, not me alone (NEVER DECIDE ALONE - surface it, don't silently pick).
- [ ] Sharpest candidates first (not one-per-lens); no hard cap - the STOP lives in Step 3 (Normal = user-paced; Auto = materiality).

## Step 2: Self-Resolve - Codebase/Memory-First (ALWAYS, token-bounded)

Canonical rule: *"if a question can be answered by exploring the codebase, explore instead."* The memory vault is the glossary.

- [ ] `mb search "<query>"` FIRST for ALL candidates - batch into few calls.
- [ ] Escalate to Grep/Glob/Read (code, logs, git) ONLY for unresolved AND decision-shaping candidates; cap ~1-2 lookups each.
- [ ] Resolved candidates are self-answered SILENTLY (recorded in Step 4, not narrated).
- [ ] Persist newly-clarified terms/decisions to the vault so the NEXT grill self-resolves them (memory = glossary; no skip-list file).
- [ ] Drop non-decision-shaping candidates instead of researching them.

## Step 3: Run the Chosen Mode over Survivors

- [ ] **Normal:**
  - [ ] Independent survivors → ONE batched AskUserQuestion (up to 4). Dependent answers (decision tree) → ONE at a time, walking the branch.
  - [ ] EVERY question carries a RECOMMENDED answer (first option, "(Recommended)") - user confirms-or-corrects.
  - [ ] **Round-based, user-paced**: small batch of the sharpest → one-line UNDERSTANDING READOUT (solid / mostly there / still fuzzy) → *"ask a couple more, or good to proceed?"* STOP on proceed OR nothing fuzzy - the user is the stop button, not a number.
  - [ ] Formal % only via `conf` (confidence-calibration) - never invent a number.
- [ ] **Auto - dig-while-material:**
  - [ ] Self-resolve each candidate (deeper code/log/vault dig). Dig while any open unknown could change the approach; stop when everything left is immaterial (wouldn't flip the plan).
  - [ ] Before stopping, ONE "what am I still missing that could matter?" sweep; resolve anything new + decision-shaping.
  - [ ] Escalate ONLY material unresolved unknowns (stuck / high-stakes), each tagged "looked at file:line, stuck because X".
  - [ ] Report a `conf` readout - shown, NOT the stop gate.
- [ ] Unanswered = `OPEN` - must be answered or deferred before the plan/design runs.

## Step 4: Record - VERY SHORT `## Grill` Block

Append to the active task note in the vault IF one exists; else inline in the following work's output. NEVER create a note just for a grill block.

```markdown
## Grill
Mode: normal | auto · {timestamp}
- Q: which flag gates this? → A: my_flag_v2, default off (service.ts:142)
- Q: "expired" = TTL or status? → A: status enum (asked user, 2026-07-03)
- Q: prod data impact? → OPEN - asked user
```

- [ ] One line each: `Q → A (source)`; source = file:line / query / "asked user, <date>" (doubles as who-decided).
- [ ] Block ≤ ~8 lines. Longer = narrating; compress.

## Gate

A SKILL.md can't enforce itself; the binding rule lives in the workflow: **before any complex analysis, plan, or design, offer grill (Auto/Normal/Skip); never auto-skip - only the user decides.** Idempotent: don't re-offer when a `## Grill` block already exists. Auto-defer ONLY during active critical-incident pain (`[➖]` "stop the bleeding first"). Grill's own duty when run: every question answered or deferred before returning control.

## Anti-Waste

Generic questions never surface (Step 1 bound + Step 2 self-resolve); self-answered questions stay silent; the vault-glossary kills repeats over time.

## Few-Shot

**Auto**: candidates → `mb search` resolves 3 silently → digs while material; sweep clean → 1 stuck material unknown escalated ("checked loader:88 + DB settings, both ambiguous") + `conf` readout → user answers → gate opens.
