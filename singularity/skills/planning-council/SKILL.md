---
name: planning-council
description: Multi-agent planning council for task decomposition, dependency mapping, risk assessment, and estimation before implementation. Spawns investigator (Decomposer), critic (Risk Finder), validator (Feasibility & Estimator), historian in adversarial parallel rounds. Triggers - "plc", "planning council", "декомпозиция", "разбей задачу", "break down", "decompose", "план задачи", "plan the task", "оцени сроки", "estimate".
---

# Planning Council — Decomposition & Estimation

Multi-agent council answering: **"What exactly needs to be done, what can go wrong, how long will it take?"** Four agents in parallel rounds; the lead (you) synthesizes between rounds and loops to convergence. Architecture decisions / trade-offs → use `council` instead.

## Hard rules (whole run)

- **User-gated launch**: run ONLY on explicit user request. If a council would help — propose it and WAIT.
- **No mid-flight stops**: after spawn, never ask the user mid-run. Decision points use **marked defaults** — apply and record `Default used: <what/why>`. User questions go before launch or into the report.
- **Anti-sycophancy is the core mechanic**: agents must attack each other's positions. Agreement without a NEW argument = `NOTHING TO ADD`. Praise is banned.
- Loop guard: hard cap **5 rounds**; not converged → report unresolved disagreements explicitly.

## Flow

```
PLC-0 Goal Contract + context → PLANNING_CONTEXT
PLC-1 Round 1 — spawn 4 agents in parallel
PLC-2 Inter-round synthesis (internal, NOT shown to user)
PLC-3 Round N+1 — re-spawn with previous-round summary
PLC-4 Convergence check → loop to PLC-2, or exit
PLC-5 Weighted-confidence synthesis + report + decision-note offer
```

---

## PLC-0: Goal Contract & Context

**Checklist (lead must check ALL before PLC-1):**
- [ ] Task extracted from the user request
- [ ] Vault searched for prior/similar work: `mb search "<topic>"`; similar tasks, decisions, pitfalls pulled into the payload
- [ ] Greenfield vs existing-flow decided. Existing flow → collect code anchors (files, entry points). **"Code already written" is NOT greenfield** — a diff shows what changed, not what correct is; the plan still needs the real baseline.
- [ ] **Grill offer (never force)**: propose the `grill` skill before the council; user decides. Skip → record `Grill: skipped by user`.
- [ ] **GOAL CONTRACT formulated** — concrete + falsifiable: `GOAL:` what "done" looks like, derived from the user's exact request (not invented); `UNMET if <X>` — observable condition under which it is NOT done; `Owner:` recorded. If it narrows the user's request → confirm BEFORE launch (pre-launch question, allowed). Agents may challenge the contract itself in Round 1.
- [ ] **Every environment claim in CONSTRAINTS came from a live probe, not from memory.** «X установлен», «Y доступен», «версия Z» проверяется `import`/`--version`/`Test-Path` в момент сборки контекста и с указанием КОНКРЕТНОГО интерпретатора (`<engine>/.venv/bin/python -c "import PIL"`, не просто `python`): «на машине есть» ≠ «в нужном venv есть». В незнакомом окружении — сначала канарейка (тривиальный no-op отдельным вызовом), потом батч; молчание команды не доказывает, что она стартовала — прочитай лог исполнителя, прежде чем писать root cause. Агенты совета вправе и обязаны перепроверять CONSTRAINTS живьём.
- [ ] PLANNING_CONTEXT assembled (template below)

**PLANNING_CONTEXT** — the SAME package to every agent, verbatim (fairness + prompt-cache reuse):
```
=== PLANNING CONTEXT ===
TASK:           [description / id]
GOAL CONTRACT:  GOAL [...] | UNMET if: [X] | Owner: [...] | Derived from: [exact user request]
SCOPE:          [repos/dirs/services involved]
CONSTRAINTS:    [deadlines, dependencies, flags]
KNOWN UNKNOWNS: [what we don't know yet]
PRIOR ART:      [similar work from vault, with paths]
CODE ANCHORS:   [existing-flow only: files/entry points — or "greenfield"]
=== END PLANNING CONTEXT ===
```

---

## PLC-1: Round 1 — Spawn 4 Agents

**Checklist:**
- [ ] All 4 spawned in a SINGLE message with 4 parallel `Agent` calls (types: `investigator`, `critic`, `validator`, `historian`)
- [ ] Each prompt = `PLANNING MISSION` marker + role overlay + PLANNING_CONTEXT + Round Contract (below)
- [ ] All 4 returned final responses

Agents fetch their own data (Read/Grep/Glob/Bash, `git log`, `mb search`). Human-only datum → `NEED DATA:`.

**Round Contract (append verbatim to EVERY agent prompt):**
```
ROUND CONTRACT:
- Number every point with your role prefix: D1, D2... (Decomposer), R1... (Risk Finder),
  F1... (Feasibility & Estimator), H1... (Historian). Reference others' points BY ID.
- End your reply with EXACTLY ONE machine-parseable final line:
    HAVE SOMETHING TO ADD   (new points raised this round)
    NOTHING TO ADD          (full agreement, no new argument)
    NEED DATA: <datum + where/who it lives with>
- Anti-sycophancy: from Round 2 on you MUST attack >=1 other agent's point by id
  ("D3 misses a dependency because <evidence>") OR reply NOTHING TO ADD.
  Agreement without a NEW argument = NOTHING TO ADD. Praise is banned.
- You may challenge the GOAL CONTRACT itself (wrong "done", not falsifiable).
- Attach a confidence % to each point (confidence-calibration skill).
```

**Role overlays:**

*Decomposer* (`investigator`):
```
PLANNING MISSION: You are the DECOMPOSER. Break the task into concrete subtasks.
- [ ] Investigated the codebase/materials — found everything that must change (file:line)
- [ ] SUBTASKS (max 10), each: SUBTASK / FILES / DEPENDS ON / TYPE (code|config|migration|test|docs)
- [ ] CRITICAL PATH identified — the longest chain of dependent subtasks
- [ ] Subtasks touching SHARED code flagged (conflict risk)
```

*Risk Finder* (`critic`):
```
PLANNING MISSION: You are the RISK FINDER. Pre-mortem the plan (Gary Klein).
- [ ] Per subtask: hidden complexity, what could break, assumptions, scope (too narrow/broad)
- [ ] PRE-MORTEM: 3 failure stories (trigger + cascade + impact), earliest warning sign each,
      the hidden assumption each rests on, one safeguard per highest-impact story
- [ ] BLOCKERS flagged (must be resolved before work starts); hidden subtasks Decomposer missed
```

*Feasibility & Estimator* (`validator`) — folded reviewer/estimator role:
```
PLANNING MISSION: You are the FEASIBILITY & ESTIMATION CHECKER.
- [ ] Verified referenced files exist and contain what is claimed; API/type assumptions checked
- [ ] Each subtask marked: FEASIBLE / NEEDS ADJUSTMENT / NOT FEASIBLE / NEEDS INVESTIGATION
- [ ] Each risk: real vs hypothetical (does existing code already handle it?)
- [ ] Missing pieces checked: migrations, config/flags, tests, cross-module impact
- [ ] Each subtask sized on the T-shirt scale: XS (<2h) / S (2-4h) / M (4-8h) / L (1-2d) / XL (2-5d)
- [ ] EVERY subtask labelled with its COST CLASS, because the two do not convert:
      `agent-bound` (writing code/text/tests — human-hour intuition overshoots the
      actual agent wall-clock by 10-20x) vs `wall-clock-bound` (model/browser
      downloads, install retries, OWNER ACCEPTANCE GATES, deliberately serialized
      work — these do not compress at all). A blanket multiplier in either
      direction is an error: dividing everything by 10-20 underestimates exactly
      the tail that carries the risk.
- [ ] Agent-bound work is quoted in AGENT SESSIONS (1 session ~ a 1-3h focused run),
      then converted to calendar days alongside the wall-clock-bound items — never
      in raw human-hours.
- [ ] TOTAL along the critical path (parallel work counts once); buffer +30% medium / +50% low confidence
- [ ] THREE-POINT estimate: BEST / LIKELY / WORST — never a single number
- [ ] What could make the estimate WRONG (unknowns, blockers)
- [ ] Token/subscription ceiling named when the plan leans on a fleet of agents:
      parallelism buys calendar time, not tokens, and on a subscription tier the
      limit bites before the deadline does.
```

*Historian* (`historian`):
```
PLANNING MISSION: You are the HISTORIAN. Prior-task and estimation scan.
- [ ] Scanned the vault via `mb search` + Grep for similar work;
      noted estimated-vs-actual effort where recorded; cited sources with dates/paths
- [ ] Per subtask: flagged known pitfalls from prior similar work
- [ ] READ-ONLY: never write/edit the vault
```

---

## PLC-2: Inter-Round Synthesis (internal)

**Checklist:**
- [ ] All outputs read IN FULL (never ask agents to trim or discard detail unread)
- [ ] Internal summary <=15 bullets, points kept under original ids (D1/R2/F3/H1...)
- [ ] Covers: subtasks + dependencies + critical path, risks (likelihood/impact), feasibility verdicts, three-point estimates, prior-art calibration, blockers/gaps
- [ ] `NEED DATA:` resolved by the lead where possible; human-only → open question in the report (default: proceed, item flagged)
- [ ] Bias check: Decomposer's first list actually challenged (no anchoring); file references verified in code; BEST/LIKELY not collapsing into one number (optimism bias); Historian's actuals compared to current estimates; shared code/APIs grepped across consumers; agent unanimity is NOT corroboration — load-bearing claims need ground truth
- [ ] Summary NOT shown to user

---

## PLC-3: Round N+1 — Re-spawn

- [ ] Round counter incremented (cap 5)
- [ ] All 4 re-spawned in parallel, single message
- [ ] Prompt = **byte-identical Round-1 prefix** (MISSION + overlay + PLANNING_CONTEXT + Round Contract, never edited) + AFTER it: `Round N. Previous-round summary: [...]` + fetched NEED-DATA answers (prompt-cache reuse)
- [ ] Suffix: "Attack or verify these points by id. Raise new risks, missing subtasks, estimate corrections — or reply NOTHING TO ADD."

---

## PLC-4: Convergence Check

- [ ] ALL 4 replied `NOTHING TO ADD` → PLC-5
- [ ] Any `HAVE SOMETHING TO ADD` / unresolved `NEED DATA:` → back to PLC-2, next round
- [ ] Round 5 without convergence → stop; report "Unresolved after 5 rounds: [ids]"

---

## PLC-5: Synthesis & Final Plan Report

**Weighted-confidence synthesis:**
- [ ] Dominant signals: feasibility pass-rate (FEASIBLE ÷ total) and three-point spread (WORST ÷ BEST)
- [ ] Points weighted by evidence class: validator-verified = full weight; asserted-unverified = half; contradicted = dropped
- [ ] One line: `Confidence: <X>% — <reason citing spread / feasibility / blockers>` per the `confidence-calibration` skill (additive model + its reversibility thresholds). Bare number = defect.
- [ ] Decision ladder: **>=60% → START**, **50–59% → SCOPE FIRST** (list investigate-first items), **<50% → DEFER** (decompose the highest-uncertainty subtask first)

**Disproval-first gate:** every subtask/risk/estimate survived one disproval attempt — uncovered code paths, assumptions that don't hold, past tasks that took longer, hidden cross-module dependencies, ordering constraints between "independent" subtasks. Only then SURVIVED.

**Final report format (HARD limits — one screen):**
- [ ] **Max ~15 bullets**: subtasks (1 line each: size, type, files, depends-on; >8 → critical-path ones + one `More: ...` line), critical path (1 line, total days), top risks + mitigations (max 3), non-FEASIBLE verdicts only, three-point estimate (1 line), blockers (or `None`)
- [ ] `Confidence: <X>% — <reason>` (verbatim from synthesis)
- [ ] **1-line decision**: `Decision: START / SCOPE FIRST / DEFER — start with <subtask>, investigate <unknowns> first`
- [ ] Header: rounds, defaults used, unresolved-after-cap
- [ ] **Exactly 1 closing question to the user**
- [ ] Per-agent breakdowns OMITTED unless asked ("show reasoning")
- [ ] **Decision-note offer (never auto-write)**: offer to save the plan as a vault decision note. If yes → `mb-memory` contract: `type: decision`, `owner` + `source` in frontmatter; body = plan, estimates, risks, UNMET condition.

---

## Final Verification (before replying to user)

- [ ] Existing agents used — investigator (Decomposer), critic (Risk Finder), validator (Feasibility & Estimator), historian — with PLANNING MISSION overlays; no custom types
- [ ] Agents spawned in parallel every round; inter-round summaries kept internal
- [ ] Decomposition preceded risk/feasibility; three-point estimate + buffers produced (never a single number)
- [ ] Loop ran to convergence (all `NOTHING TO ADD`) or the 5-round cap with disagreements surfaced
- [ ] Anti-sycophancy enforced: every round-2+ output attacked a point by id or was `NOTHING TO ADD`
- [ ] No mid-flight user stops; defaults marked in the report
- [ ] Every checkbox checked or `[-]` N/A with reason — none silently skipped
