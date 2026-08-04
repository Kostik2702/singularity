---
name: council
description: Multi-agent architectural council for design decisions, implementation approaches, and technical strategy. Spawns investigator, critic, validator, historian in adversarial parallel rounds with a consensus loop. Triggers - "council", "совет", "созови совет", "архитектурное решение", "X vs Y", "trade-off", "trade-offs", "which approach", "какой подход лучше", "design decision", "стоит ли переходить на".
---

# Architectural Council — Decision Making

Multi-agent council for **architecture decisions, approaches, trade-offs, strategy**. Four agents run in parallel rounds; the lead (you) synthesizes between rounds and drives a consensus loop until convergence.

**For task decomposition/estimation** → use `planning-council` ("plc") instead.

## Hard rules (apply to the whole run)

- **User-gated launch**: the council runs ONLY on explicit user request. If you think a council would help — propose it and WAIT. Never auto-launch.
- **No mid-flight stops**: once agents are spawned, never ask the user questions mid-run. Any decision point uses a **marked default** — apply it, record `Default used: <what/why>` for the final report. Questions to the user go BEFORE launch or into the final report.
- **Anti-sycophancy is the core mechanic**: agents are obliged to attack each other's positions. Agreement without a NEW argument = `NOTHING TO ADD`. Praise is banned in agent outputs.
- Loop guard: hard cap **5 rounds**. Not converged after 5 → report unresolved disagreements explicitly.

## Flow

```
Phase 0: Goal Contract + context → CONTEXT_PAYLOAD
Phase 1: Round 1 — spawn 4 agents in parallel
Phase 2: Inter-round synthesis (internal, NOT shown to user)
Phase 3: Round N+1 — re-spawn with previous-round summary
Phase 4: Convergence check → loop to Phase 2, or exit
Phase 5: Weighted-confidence synthesis + final report + decision-note offer
```

---

## Phase 0: Goal Contract & Context

**Checklist (lead must check ALL before Phase 1):**
- [ ] Topic extracted from the user request
- [ ] Vault searched for prior knowledge: `mb search "<topic>"` (vault = `{{VAULT}}`); relevant decisions/howtos/incidents pulled into the payload
- [ ] **Grill offer (offer, never force)**: propose running the `grill` skill on the topic before the council. User decides (run / skip). Skip = fine, record `Grill: skipped by user`.
- [ ] **GOAL CONTRACT formulated** — concrete + falsifiable:
  - `GOAL:` derived from the user's exact request (not invented)
  - `UNMET if <X>` — an observable condition under which the goal is NOT met
  - `Owner:` who owns the outcome (usually the user)
  - If the contract narrows/sharpens the user's request → confirm with the user BEFORE launch (this is a pre-launch question, allowed)
  - Agents are explicitly entitled to challenge the contract itself in Round 1
- [ ] CONTEXT_PAYLOAD assembled (template below)

**CONTEXT_PAYLOAD** — the SAME package goes to every agent, verbatim (fairness + prompt-cache reuse):
```
=== COUNCIL CONTEXT ===
Topic: [problem]
BACKGROUND: [2-3 sentences]
PRIOR KNOWLEDGE (vault): [decisions/notes found via mb search, with paths]
CODE/DOC ANCHORS: [paths where the topic lives, if any]
OPTIONS ON THE TABLE: [A / B / ... if the user named them]
GOAL CONTRACT:
  GOAL: [...]  |  UNMET if: [X]  |  Owner: [...]  |  Derived from: [exact user request]
OPEN QUESTIONS: [what to decide]
=== END CONTEXT ===
```

---

## Phase 1: Round 1 — Spawn 4 Agents

**Checklist:**
- [ ] All 4 agents spawned in a SINGLE message with 4 parallel `Agent` calls (subagent types: `investigator`, `critic`, `validator`, `historian`)
- [ ] Each prompt = `COUNCIL MISSION` marker + role overlay + CONTEXT_PAYLOAD + Round Contract (below)
- [ ] All 4 returned final responses (that is how the lead reads them)

Agents fetch their own data (Read/Grep/Glob/Bash, `mb search`). If a datum is human-only, they reply `NEED DATA:`.

**Round Contract (append verbatim to EVERY agent prompt):**
```
ROUND CONTRACT:
- Number every point with your role prefix: I1, I2... (investigator), C1... (critic),
  V1... (validator), H1... (historian). Reference other agents' points BY ID.
- End your reply with EXACTLY ONE machine-parseable final line:
    HAVE SOMETHING TO ADD        (you raised new points this round)
    NOTHING TO ADD               (full agreement, no new argument)
    NEED DATA: <datum + where/who it lives with>
- Anti-sycophancy: from Round 2 on you MUST attack at least one other agent's point
  by id ("C2 is wrong because <evidence>") OR reply NOTHING TO ADD.
  Agreement without a NEW argument counts as NOTHING TO ADD. Praise is banned.
- You may challenge the GOAL CONTRACT itself (wrong goal / not falsifiable / wrong UNMET).
- Attach a confidence % to each point per the confidence-calibration skill.
```

**Role overlays** (MISSION line first, then overlay, then payload + contract):

*Investigator* (`investigator`):
```
COUNCIL MISSION: Deep research and evidence gathering for an architecture decision.
- [ ] TOP 3 findings with file:line / doc path, confidence % each
- [ ] Each finding survives >=1 self-disproval attempt before you report it
- [ ] Gaps in knowledge documented explicitly
```

*Critic* (`critic`):
```
COUNCIL MISSION: Challenge assumptions, find risks, devil's advocate.
- [ ] Asked: is this the right problem? symptom vs root cause?
- [ ] Found >=3 gaps or risks; proposed an alternative where you disagree
- [ ] For each option: a realistic failure scenario 3 months post-adoption
```

*Validator* (`validator`) — also carries feasibility (folded reviewer role):
```
COUNCIL MISSION: Fact-check every claim against actual code/docs + assess feasibility.
- [ ] Each claim marked: VERIFIED / INACCURATE / FALSE / UNVERIFIABLE, with file:line evidence
- [ ] Complexity rated per option: Low / Medium / High
- [ ] Risks vs benefits per option; own recommendation: PROCEED / INVESTIGATE MORE / DEFER
```

*Historian* (`historian`):
```
COUNCIL MISSION: Prior art, reuse analysis, historical context.
- [ ] Answered: decided before? what precedent? Scan the vault ({{VAULT}})
      via `mb search` + Grep; cite sources with dates/paths
- [ ] READ-ONLY: never write/edit the vault
```

---

## Phase 2: Inter-Round Synthesis (internal)

**Checklist:**
- [ ] All agent outputs read IN FULL (never instruct agents to trim; never discard detail unread)
- [ ] Internal summary compiled, <=15 bullets, points kept under their original ids (I1/C2/V3/H1...)
- [ ] Covers: claims + evidence, disagreements (by id), VERIFIED/FALSE verdicts, feasibility, prior art, gaps
- [ ] `NEED DATA:` lines resolved: lead fetches the datum itself (Bash/Read/Grep/mb search) and injects into the next round; human-only data → recorded as an open question for the final report (marked default: proceed without, flag it)
- [ ] Bias check: no anchoring on the first hypothesis; load-bearing claims corroborated by ground truth (file:line/doc), not by agent agreement alone — Claude-family unanimity is NOT corroboration; someone asked "what if we DON'T do this?"
- [ ] Summary NOT shown to user (fuel for the next round, not a report)

---

## Phase 3: Round N+1 — Re-spawn

**Checklist:**
- [ ] Round counter incremented (cap 5)
- [ ] All 4 re-spawned in parallel, single message
- [ ] Each prompt = **byte-identical Round-1 prefix** (MISSION + overlay + CONTEXT_PAYLOAD + Round Contract — never edited/reordered) + appended AFTER it: `Round N. Previous-round summary: [...]` + fetched NEED-DATA answers. Enables prompt-cache reuse.
- [ ] Suffix instruction: "Attack or verify these points by id. Raise new findings/corrections/contradictions, or reply NOTHING TO ADD."

---

## Phase 4: Convergence Check

- [ ] ALL 4 replied `NOTHING TO ADD` → Phase 5
- [ ] Any `HAVE SOMETHING TO ADD` / unresolved `NEED DATA:` → back to Phase 2, next round
- [ ] Round 5 reached without convergence → stop; Phase 5 must list "Unresolved after 5 rounds: [ids]"

---

## Phase 5: Synthesis & Final Report

**Weighted-confidence synthesis:**
- [ ] Each surviving recommendation point weighted by evidence class: validator-VERIFIED (ground truth cited) = full weight; asserted-but-unverified = half; contradicted = dropped
- [ ] One line synthesized: `Confidence: <X>% — <reason>` per the `confidence-calibration` skill (additive model; use its reversibility thresholds — hard-to-reverse decisions need the higher band)
- [ ] Decision ladder: **>=70% → PROCEED**, **60–69% → INVESTIGATE MORE**, **<60% → DEFER**. Bare `Confidence: 80%` without reasoning = defect.

**Disproval-first gate (before the report):** the winning recommendation survived at least one structured disproval attempt — a scenario where it fails was actively searched for (critic + validator), rejected alternatives' "why not" re-checked, hidden costs searched. Only then mark it SURVIVED.

**Final report format (HARD limits — one screen):**
- [ ] **Max ~15 bullets total**: agreements (decisive only), disagreements (or `None`), verified/contradicted claims (1 line each, file:line), key risks
- [ ] `Confidence: <X>% — <reason>` (verbatim from synthesis)
- [ ] **1-line decision**: `Decision: PROCEED / INVESTIGATE MORE / DEFER — <one line why>`
- [ ] Header: rounds run, defaults used, unresolved-after-cap (if any)
- [ ] **Exactly 1 closing question to the user** (next step / open fork)
- [ ] Per-agent breakdowns OMITTED unless the user asks ("show reasoning")
- [ ] **Decision-note offer (never auto-write)**: "Want me to save this as a decision note in the vault?" If yes → write via the `mb-memory` skill contract: `type: decision`, `owner` + `source` in frontmatter, body = decision, alternatives rejected + why, confidence, UNMET condition.

---

## Final Verification (before replying to user)

- [ ] Existing agents used (investigator/critic/validator/historian) with COUNCIL MISSION overlays — no custom types
- [ ] Agents spawned in parallel every round; inter-round summaries kept internal
- [ ] Loop ran to convergence (all `NOTHING TO ADD`) or the 5-round cap with disagreements surfaced
- [ ] Anti-sycophancy enforced: every round-2+ output either attacked a point by id or was `NOTHING TO ADD`
- [ ] No mid-flight user stops; all defaults marked in the report
- [ ] Every checkbox in this skill checked or marked `[-]` N/A with reason — none silently skipped
