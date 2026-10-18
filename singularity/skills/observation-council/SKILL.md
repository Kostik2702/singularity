---
name: observation-council
description: 'Hypothesis-driven root cause investigation (SRE-style multi-agent council) for bugs, incidents, strange behavior. Triggers: oc, observation council, "why is X happening", "why does X do Y", strange behavior, not working, incident, regression, silent failure, figure out why it broke.'
disable-model-invocation: true
---

Starts only on the owner's explicit command. If you think a council is needed, ask the owner to run it and say why.

# Observation Council — Hypothesis-Driven RCA

Multi-agent RCA for bugs and incidents (Google SRE hypothesis-driven debugging): generate competing hypotheses → each makes testable predictions → actively try to DISPROVE every hypothesis with data → the survivor is the root cause. **Data decides, not votes** — agent agreement is never evidence; every surviving hypothesis needs a ground-truth anchor (log line, commit, file:line, reproduced output).

Routing: architecture/trade-off → `council`; hypothesis known → just debug; broad research → investigation. Use this when the CAUSE is unknown.

## Launch gate (user-gated)

The council spawns multiple agents — never start it silently:
- [ ] User invoked it ("oc ...") OR you proposed the council and the user agreed.
- [ ] GOAL CONTRACT shown before spawning: `GOAL: <what we must know to declare this resolved>. UNMET if <X>.` Derived from the user's words; if it narrows their request — confirm first. The goal bounds the QUESTION, not which rival hypotheses may be pursued.
- [ ] After launch — NO mid-flight stops: never pause a round to ask the user; take the marked default, tag it `[default]`, surface it in the final report. Defaults: unavailable data → 🔒 NEEDS DATA; ambiguous scope → wider; risky reproduction → skip, note untested.

## OC-0: Structured Observation

- [ ] Symptom with specifics — not "slow" but "200ms → 12s"; exact error text; baseline (what normal looks like).
- [ ] Time boundary (started when / last known good) + scale. "Sometimes fails" must be quantified before spawning — never run a hand-wavy council.
- [ ] What changed: `git log --since=<last-good>`, config edits, dependency/env changes.
- [ ] Prior incidents: `mb search "<symptom>"` (vault incidents, rules, learnings) — attach hits with dates as BACKGROUND.
- [ ] **Inverse Trust Rule** (mandatory pre-pass): check the SIMPLEST cause FIRST, before any agent spawns — your own fresh edit, config/env change, stale cache or build artifact, typo, wrong directory/branch/venv. Each takes minutes via `git diff` / reproduction. Only the unexplained residue goes to the council. Record what was ruled out.
- [ ] Critical gaps → ask the user NOW (no questions allowed later).
- [ ] P0 (actively blocking): stabilize first (revert/disable/workaround), preserve evidence BEFORE stabilizing (copy logs, dump state); triage and RCA run in parallel.

OBSERVATION template (verbatim to every agent):
```
=== OBSERVATION ===
WHAT is broken:      [exact symptom / error text]
WHEN / last good:    [timestamp, commit, or "unknown"]
SCALE / repro:       [always / intermittent+rate / inputs]
BASELINE:            [normal behavior]
WHAT CHANGED:        [git shas, config, deps, environment]
SEVERITY:            P0 blocking / P1 major / P2 degraded / P3 minor
GOAL:                [...] UNMET if <X>
INVERSE-TRUST PASS:  ruled out [edit/config/cache/typo] because [evidence]
BACKGROUND:          [mb search hits, prior incidents with dates]
=== END ===
```

## Data sources (agents fetch themselves — never ask the user mid-round)

Local logs (`.mb/*.log`, app logs) · `git log/diff/blame` · `mb search` (prior incidents, rules, learnings) · reproduction runs (execute the failing flow, capture output) · command output (versions, env, config). A prediction no source can test → `🔒 NEEDS DATA: <exact command or user question>`; it stays inconclusive — a hypothesis cannot win the verdict on it.

## OC-1: Round 1 — spawn agents in parallel

- [ ] All agents spawned in ONE message (parallel Agent calls, each with `model: "opus"`), each with the full OBSERVATION + role overlay; every overlay starts with the `SRE MISSION:` marker line.
- [ ] Roles = agent definitions investigator / critic / validator (`C:\Users\Admin\.claude\agents\`); definition missing → fall back to `general-purpose` with the same overlay.

### Hypothesis Generator — subagent_type: `investigator`
```
SRE MISSION: You are the HYPOTHESIS GENERATOR. Hypothesis-driven debugging.
[OBSERVATION payload]
- [ ] Exactly 3 COMPETING hypotheses; at least 1 non-obvious wild card
- [ ] ≥1 grounded in WHAT CHANGED — but a timing match alone is marked
      "TIMING CORRELATION ONLY" until the mechanism is verified with non-code data
- [ ] Each H: HYPOTHESIS / MECHANISM / PREDICTION ("if true, we should also see X")
      / EVIDENCE FOR (file:line) / EVIDENCE AGAINST / CONFIDENCE %
- [ ] Status per H: ALIVE / WEAKENED / DEAD (by evidence balance)
- [ ] Bisection on ≥1 H: halve the search space (time window, code path, config)
      and record the boundary check
```

### Falsifier — subagent_type: `critic`
```
SRE MISSION: You are the FALSIFIER. Your ONLY job: DISPROVE every hypothesis.
You succeed when hypotheses die.
[OBSERVATION payload]
Per hypothesis:
- [ ] Take its prediction; search code/logs/git for evidence that CONTRADICTS it
- [ ] Find alternative explanations for its "evidence for"
- [ ] Ask: ROOT CAUSE or just a SYMPTOM of something deeper?
- [ ] Ask: what scenario would make this H IMPOSSIBLE? Check it.
- [ ] Output per H: FALSIFICATION ATTEMPT + RESULT (DISPROVED/SURVIVED/WEAKENED,
      with evidence) + DEEPER QUESTION
- [ ] If every current H looks wrong — propose a counter-hypothesis
```

### Prediction Tester — subagent_type: `validator`
```
SRE MISSION: You are the PREDICTION TESTER. Verify predictions against actual
code and system behavior.
[OBSERVATION payload]
Per prediction:
- [ ] Test against real data (run it, read code, git, logs):
      CONFIRMED / REFUTED / INCONCLUSIVE / NEEDS DATA (exact command or user question)
- [ ] Scorecard per H: N confirmed / M refuted / K inconclusive
- [ ] Test in decreasing order of likelihood; note the risk of each test
- [ ] Confounding scan per CONFIRMED: name ≥1 alternative cause producing the
      same observation (cache, env var, PATH, encoding); CONFOUNDED if not excludable
- [ ] Any test that manipulates the system (cache clear, restart, config flip) →
      tag result POST-MANIPULATION so later rounds don't treat it as baseline
```

## OC-2: Inter-round synthesis (lead, internal — NOT shown to user)

- [ ] All agent outputs read in full; ≤12-bullet internal summary: H statuses + confidence, scorecard, falsification results, open gaps.
- [ ] Bias guard — false correlation: a change date matching the incident date is the most seductive trap, verify the mechanism before promoting; confirmation: evidence AGAINST is sought, not just FOR; anchoring: all effort on H1 → force alternatives.
- [ ] Independent sources: each promoted fact rests on 2+ independent sources (code+log, git+reproduction). Code analysis alone is ONE source — it cannot both generate and confirm a hypothesis.
- [ ] Data decides, not votes: every surviving H has a ground-truth anchor (log line / commit / file:line / reproduced output). Unanimity without an anchor → UNVERIFIED, route to the Prediction Tester before it may win.

## OC-3: Rounds and convergence

- [ ] Round N+1: re-spawn all agents in parallel (Agent calls with `model: "opus"`). Prompt = byte-identical Round-1 prefix (overlay + OBSERVATION, never edited) + appended inter-round summary (prompt-cache reuse).
- [ ] Machine-parseable round contract — every agent MUST end its reply with exactly one line:
  - `HAVE SOMETHING TO ADD: <one line — new hypothesis / disproof / status change>`
  - `NOTHING TO ADD` — full agreement, nothing new
  - `NEED DATA: <exact command or user question>` — blocked on a fetchable datum
- [ ] All `NOTHING TO ADD` → OC-4. Any `HAVE SOMETHING TO ADD` → next round. `NEED DATA` → lead fetches it (or applies the 🔒 default) and re-spawns that agent.
- [ ] Hard cap 5 rounds; not converged → surface disagreements explicitly in the verdict.
- [ ] Early exit after Round 1 only if ALL: full mutual agreement, one clear root cause with a ground-truth anchor, no 🔒 gap on the winner, severity not P0/P1.

## OC-4: Verdict with calibrated confidence

- [ ] Winner = the H that SURVIVED falsification with the best prediction scorecard — never the most "voted".
- [ ] Confidence via the `confidence-calibration` skill: anchor on the winner's scorecard (confirmed ÷ predictions made), adjust down per ⚠️/🔒 gap; paste verbatim as `<X>% — <reasoning>` (bare number = defect).
- [ ] Ladder: ≥70% → single Root Cause Winner; 60–69% → INCONCLUSIVE (top-2 co-leading); <60% → DEFER (one more prediction-test round; at cap → report unresolved).
- [ ] Full chain check: mechanism traced end-to-end trigger → symptom, no "and then somehow" gap. A timeout/hang is itself a SYMPTOM — trace what the layer below actually returned.

## OC-5: Report + incident note offer

Chat report (one screen max):
```markdown
## RCA: [problem]
**Severity**: Px | **Rounds**: N (+early exit) | **Goal**: MET / UNMET (<X>)
### Hypothesis Matrix
- H1: [statement] — predictions N / confirmed / refuted / ALIVE|DEAD / conf %
### Root Cause Verdict
**Winner**: H[n] — [statement]
**Confidence**: [X]% — [reasoning, verbatim from calibration]
**Mechanism**: [trigger → ... → symptom, file:line]
**What killed the alternatives**: [one line per dead H + evidence]
### Fix
[what to change] — complexity: trivial/moderate/major | risk | files
### Unresolved
- [needs-data gaps, inconclusive predictions, every `[default]` taken]
```

- [ ] Then OFFER — never auto-write: "Record an incident note in the vault?" On yes → `mb note new incident "<title>"` (vault `templates/incident.md`), fill the template's sections: symptom / timeline / hypotheses and refutations (every H + how it died) / root cause / fix and prevention. Repeatable assistant mistake exposed → also offer a `rule` note (`mb-memory` skill).

## Disproval table

| Hypothesis type | How to disprove |
|---|---|
| "Recent change X broke it" | Did it fail BEFORE X? Checkout prior sha, reproduce |
| "Stale cache / artifact" | Clear once, retest; persists → H dead (POST-MANIPULATION) |
| "Race / intermittent" | Find the exact window; deterministic repro? Quantify "sometimes" |
| "Bad data" | Check upstream — already wrong before this code ran? |
| "Environment / config" | Diff env vs known-good state; run with clean env |

## Final verification

- [ ] Inverse Trust pass done BEFORE spawning; simplest causes ruled out with evidence.
- [ ] Every surviving H had ≥1 structured falsification attempt; every dead H's cause of death recorded.
- [ ] Verdict anchored to ground truth, not agent agreement; confidence pasted from calibration.
- [ ] No mid-flight user stops; all `[default]` choices surfaced under Unresolved; incident-note OFFERED, not auto-written.
- [ ] Every checkbox in this skill resolved `[x]` or `[➖]` with reason — none silently skipped.
