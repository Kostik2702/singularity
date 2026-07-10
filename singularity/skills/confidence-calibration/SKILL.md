---
name: confidence-calibration
description: Evidence-based confidence for conclusions, root causes, decisions. Additive model (sum 100%), disproval-first verification, thresholds by reversibility. Triggers: conf, confidence, how sure, уверенность, насколько уверен.
allowed-tools: Read, Bash, Grep, Glob
---

# Confidence Calibration

**Confidence is built UP from 0%, one verified box at a time. Max 100%. Verified box adds its %; unverified adds 0.** Never an intuitive number.

## ACCOUNTING MODEL (Strict Additive, Σ = 100%)

Per-box % = section budget / boxes. No caps, no subtracted penalties. Score = SUM of verified boxes.

| Section | Budget | Boxes | +%/box |
|---|---|---|---|
| L1: Read Code | 14% | 7 | +2% |
| L2: Static Analysis | 18% | 12 | +1.5% |
| L3: Confirming Test | 12% | 8 | +1.5% |
| L4: Dynamic Verification | 22% | 11 | +2% |
| L5: Reproduction | 14% | 7 | +2% |
| Inverse Trust (basics) | 4% | 8 | +0.5% |
| Red Flags (verified clean) | 7% | 7 | +1% |
| Cognitive Bias Guard | 5.5% | 11 | +0.5% |
| Log Analysis (only if logs) | 3.5% | 5 | +0.7% |
| **TOTAL** | **100%** | **76** | - |

Rules: each concept in EXACTLY ONE section. Contradictions are STRUCTURAL - invalidate the box, force re-investigation. Hallucinated file/function blocks L1 "Verified exists". N/A section → all boxes `[➖]`, budget forfeit (max = 100% - budget); do NOT redistribute.

**Search default**: `mb search "<query>"` (memory vault) BEFORE Grep/Glob; Grep = fallback for exact literals.

## GRILL ORDER - Kill-Questions First (changes order/stop only, never the math)

1. List 3-5 kill-questions most likely to FALSIFY *this* claim (see strategies), ordered by decision-criticality of the premise, NOT cheapness.
2. Ask them first - seek contradicting evidence (mb search → grep → run/query).
3. Stop ONLY on affirmative contradiction: FAILED needs real evidence (file:line/query/log). "Did not confirm"/"inconclusive" is NOT a kill - box stays 0, walk CONTINUES.
4. Weight-aware kill - contradicting evidence must be at least as strong as what it forfeits (a -2% source can't alone kill a +3% box).
5. Survivors are NOT pre-credited - full walk follows. FAILED records the killing file:line.

## CORE METHOD: DISPROVAL-FIRST LOOP

```
1. STATE the argument
2. TRY TO DISPROVE - grep contradicting paths; git log; edge cases;
   alternatives; does the evidence prove what I think?
3. GAPS? YES → fill → back to 2 | NO → CHECKED
```
Track: `ARGUMENTS: 1..N [ ] NOT VERIFIED` + per-argument log (attempt → found → gap → action → CHECKED/FAILED). Never CHECKED on first pass (min 1 disproval attempt); counter-evidence = FAILED, don't rationalize; "I didn't look" ≠ "no counter-evidence"; filling a gap RESETS disproval; FAILED loses full weight.

### Disproval Strategies

| Argument | Disprove by |
|---|---|
| "Bug is in X:Y" | Grep all callers; git blame - recently changed? |
| "This is the root cause" | 3 alternative hypotheses - any fit better? |
| "Fix will work" | Edge cases, breaking inputs, side effects |
| "No other paths affected" | Grep ALL usages; inheritance; dynamic dispatch |
| "Flag/config controls this" | State in ALL envs; conditions/overrides |
| "Analysis is complete" | Walk backwards - missed entries? listeners? cron? |
| "Line was checked" | Re-read in full context; what if it throws? |
| "I reproduced it" | Manipulated before/during? State recreated? Repeat untouched |
| "Alternative H implausible" | Implausibility ≠ disproof. Cite evidence making H IMPOSSIBLE, else H is ALIVE, confidence capped |
| "Observation X confirms H" | List every OTHER mechanism producing X; any not excluded → SHARED signal |

## INVERSE TRUST (8 x +0.5%)

*LLM fails at basics more than complex.* Did-I-look checks:

- [ ] OPENED the file (vs recall)?
- [ ] Copied paths/identifiers verbatim?
- [ ] Copied file:line directly from the file?
- [ ] Looked up the import's actual package name?
- [ ] Confirmed repo/branch/working-tree state?
- [ ] Opened the actual DB schema?
- [ ] Opened the API spec / route file?
- [ ] Checked field persistence - DB vs in-memory vs computed?

## RED FLAGS (7 x +1%, adds when confirmed ABSENT)

- [ ] No complex analysis without file:line
- [ ] No high confidence without verification
- [ ] No long explanation without running the code
- [ ] No "should work" / "probably"
- [ ] No fix without reproducing the bug
- [ ] No quick answer without reading files
- [ ] No memory-of-code instead of current state

## COGNITIVE BIAS GUARD (11 x +0.5%)

- [ ] Anchoring - 2+ alternatives considered
- [ ] Sunk cost - would still pursue starting fresh
- [ ] Confirmation - searched AGAINST conclusion first
- [ ] False correlation - mechanism, not timing overlap
- [ ] Availability - frequency data, not dramatic recent event
- [ ] Over-extrapolation - stated boundaries ("Confirmed for X, not Y")
- [ ] Confounding - ≥1 alt mechanism for the SAME signal; not excludable → CONFOUNDED
- [ ] Compound-cause (Hickam) - 2+ weaker causes fit better together?
- [ ] Actor timing - exact timestamps of ALL candidates; earliest = cause, not loudest
- [ ] Timing disproof - "A caused B" backed by non-temporal evidence
- [ ] Intent confirmed - ambiguous task → 2 interpretations + cost diff to user BEFORE picking

## VERIFICATION LEVELS

### L1: Read Code (7 x +2%)
- [ ] File:line reference found
- [ ] Verified exists (ls/grep)
- [ ] Hallucination check - file/function real
- [ ] Deps refreshed before claiming something missing
- [ ] Numbers/config cited from source, never memory
- [ ] Variant scope - holds for ALL variants, not only where symptom seen
- [ ] Method-call resolution - actual chain; conditional delegation skips calls

### L2: Static Analysis (12 x +1.5%)
- [ ] Git history for recent changes
- [ ] ALL usages/callers grepped
- [ ] Flags/config state verified
- [ ] Stale info check (git log -1)
- [ ] Partial view - ALL usages, not just obvious
- [ ] Missing context - env vars, config
- [ ] Name vs implementation - names mislead
- [ ] Result-pick determinism - `.first`/`[0]` have explicit ordering
- [ ] Serialization - field survives cache/queue/RPC/DB/JSON
- [ ] Filters not coupled to platform-owned identity that can be null
- [ ] Cross-repo order - publisher releases BEFORE consumers bump
- [ ] Env-specific state queried per env (staging ≠ prod)

### L3: Confirming Test (8 x +1.5%)
- [ ] Existing test validates behavior
- [ ] 3+ alternative hypotheses
- [ ] ALL similar patterns/endpoints checked
- [ ] Differentiating test - rules ≥1 alternative OUT, else SHARED signal
- [ ] Negative-results - each rejected alternative has cited disproof
- [ ] Expert pivot - a source points elsewhere → trace THEIR trail first
- [ ] Symptom directionality - tested the OTHER direction
- [ ] Production-scale validation - prod volumes, not dev sets

### L4: Dynamic Verification (11 x +2%)
- [ ] Ran code/test
- [ ] DB query confirmed data state
- [ ] Traced flow to verify logic
- [ ] Queried actual state, not assumed
- [ ] Temporal gap - re-verified before final claim
- [ ] Asymmetric ops - opposite path doesn't inherit/bypass the gate
- [ ] Running version contains the claimed fix (build/commit)
- [ ] Time budgets - inner retry x delay fits outer timeout
- [ ] Cache lifecycle - BOTH TTL AND invalidation paths
- [ ] Capacity vs traffic - errors BEFORE peak = capacity, not surge
- [ ] Observability - metrics for EVERY component; gaps flagged

### L5: Reproduction (7 x +2%)
- [ ] Reproduced the issue myself
- [ ] 3+ INDEPENDENT sources converge (logs+DB+metrics; code = ONE source)
- [ ] Path-dependent state recreated explicitly
- [ ] Not contaminated - untouched baseline, no mid-test manipulation
- [ ] Object identity - SAME instance, not copies
- [ ] End-to-end trace - one request ID entry→exit
- [ ] Multi-layer match - trigger and action layers filter COMPATIBLY

### Log Analysis (only if logs, 5 x +0.7%)
- [ ] FINAL/post-processing log, not intermediate stage
- [ ] Math adds up before proposing fix
- [ ] Round numbers questioned as limits/truncation
- [ ] Timeline - business changes, not just code
- [ ] Truncation checked before concluding "missing"

## EVIDENCE WEIGHTS (contradictions)

Contradicting evidence reverts the box (+ forfeit) AND deducts per instance:

| Contradicting evidence | Deduction |
|---|---|
| Direct code (file:line) | -3% |
| Git history / vault docs / ran snippet / DB query / reproduced error | -2% each |

Re-verify after resolution; the deduction returns only if the contradiction was a misread.

## THRESHOLDS

| Action | Required | Example |
|---|---|---|
| Documentation only | 70% | Findings |
| Reversible (<5 min) | 82% | Config/flag change |
| Semi-reversible | 88% | Staging deploy |
| Hard to reverse | 93% | Prod deploy |
| Irreversible | 98% | DB migration |

| Severity | Reversible | Irreversible |
|---|---|---|
| P0 | 78% | 92% |
| P1 | 82% | 95% |
| P2+/Normal | 85-88% | 98% |

Rules: **<98%** - state what's missing, investigate; **<90%** - present as "possible issue, needs verification"; **when challenged** - "My confidence is now [X]% - re-investigating"; below threshold - list gaps → investigate until cleared OR document uncertainty.

**Stop point**: walk boxes in evidence-per-token order; MAY stop once the sum clears the threshold, ONLY if (a) no unwalked box is contradiction-capable for a credited one (its evidence type is in the Weights table); (b) **≥93% tier = full 76-box walk, no early stop**. **Stake-drift**: a box reveals higher stakes → re-classify up a tier. Skipped boxes = `[ ]` (0%), note the early-stop.

## AUTO-TRIGGER

Run EVERY TIME you: propose a decision/solution; state a root cause; recommend an approach; claim "will work"/"is the problem"; make architectural/factual claims. Always state the %.

## OUTPUT FORMAT

**Summary Line (mandatory first line)**: `<X>% - because <1-2 top evidence signals>`

**<98% → add Gaps Structure**:
```
### Closed Items
- ✅ <file:line / query / test - concrete signal>
### Unresolved Gaps
- [ ] <concrete action - read file:line / query / verify>
### Why Not Closed (STRICT)
- gap 1: <tool ATTEMPTED + failure mode>, OR one LEGITIMATE blocker
```

**Anti-laziness gates** - every gap must show an ATTEMPT, not a skip. Per gap: FORBIDDEN `[x]` if matched (= lazy, go attempt), `[➖]` if not; exactly ONE LEGITIMATE `[x]` or tool + failure mode:
- [ ] FORBIDDEN: "out of scope" / "deferred" / "no time"
- [ ] FORBIDDEN: "would require <X>" / "no prod data access" (attempt, record the failure)
- [ ] FORBIDDEN: "beyond scope of files reviewed" / "static review only"
- [ ] LEGITIMATE: needs credentials/auth unobtainable this session
- [ ] LEGITIMATE: needs interactive run of a local GUI app
- [ ] LEGITIMATE: blocked on a human answer
- [ ] LEGITIMATE: needs a deploy / remote-system action

**≥98% → add per-section table**: Section | Total | Evaluated | True | Failed | Impact (Impact = True x per-box value; N/A → Evaluated 0, Impact `[➖]`).

## DECOMPOSITION (Complex Claims)

```
Diagnosis:     [X]% - root cause correct?
Solution:      [Y]% - fix will work?
Safety:        [Z]% - no side effects?
Reversibility: [R]% - can undo?
Overall = min(X, Y, Z)
```

## COMPLETE CHECKLIST (mandatory)

Walk EVERY item in EVERY applicable section: `[x]` verified (+%), `[ ]` not verified (0), `[➖]` N/A with reason. Early stop only per Stop point; Hard-to-reverse / Irreversible = full walk.
