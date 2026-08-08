# Incoming Fork — Evidence Baseline (T00)

Status: **source unavailable — documentary characterization matrix**.
Scope: [[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] task T00.
Canonical business/technical contract: [[Singularity Incoming Fork Integration Specification]].
Decision record: [[Integrating the incoming fork into Singularity]].

## 1. Source availability check (this session, 2026-07-12)

Fork commits `a00a050..c5f6715` are not resolvable against this repository:

```text
git cat-file -t a00a050  -> fatal: Not a valid object name a00a050
git cat-file -t c5f6715  -> fatal: Not a valid object name c5f6715
git remote -v            -> origin only (github.com/Kostik2702/singularity.git)
git branch -a            -> feat/audit-backlog, feat/codex-integration,
                             feat/gemini-cli-integration, fix/index-gen-token-collision,
                             master, origin/* mirrors of the same — no fork branch
```

No archive, zip, or secondary checkout of the fork exists under `D:\Singularity`. The
external transfer folder used to pass fork documents to the prior analysis session is
explicitly recorded as **not a source of truth** ([[USER-INCOMING-HOOKS]], entry
2026-07-12 — Canonical artifact correction). Conclusion: **source is confirmed
unavailable**, not merely unsearched. No code is ported in this task; only current
upstream behavior is verified and fork-only claims are marked documentary.

## 2. Current-upstream inventory (code-verified, this session)

| # | Capability | Source (file:line, re-verified) | Test evidence | Status |
|---|---|---|---|---|
| 1 | `auto_capture` default `true` | `singularity/config.py:38-50` (key at :46) | `pytest -q` full suite | VERIFIED |
| 2 | `auto_capture` boolean type validation | `singularity/config.py:91-92` | full suite | VERIFIED |
| 3 | `mb config get/set` atomic write | `singularity/cli.py:245-280` (`cmd_config`) | full suite | VERIFIED |
| 4 | Shared capture decision core (`auto_capture_enabled`, `session_marker`, `should_remind`, `MIN_TOOL_USES=8`) | `singularity/hooks/capture_decision.py:1-45` | full suite | VERIFIED |
| 5 | Claude Stop blocks once, tool-count + substring capture heuristic (`"mb learn"` / vault-path substring in `Write`/`Edit` lines) | `singularity/hooks/stop_capture.py:37-95` | full suite | VERIFIED |
| 6 | Gemini AfterAgent passive `systemMessage`, headless-safe (no `deny`) | `singularity/hooks/gemini_after_agent.py:38-116` | full suite | VERIFIED |
| 7 | SessionStart is a separate hook, not gated by `auto_capture` at all | `singularity/hooks/session_start.py:26-119` | full suite; also directly observed as this session's own SessionStart banner | VERIFIED |
| 8 | Claude registers SessionStart + Stop as owned (`"_mb": true`) entries | `install/register_hooks.py:17-36` | full suite | VERIFIED |
| 9 | Gemini registers SessionStart + AfterAgent as named owned hooks, malformed settings left untouched | `install/register_gemini.py:169-196` | full suite | VERIFIED |
| 10 | Codex gets `AGENTS.md` managed block + skill junctions only, **no runtime hook** | `install/register_codex.py:1-19` | full suite | VERIFIED |
| 11 | Full current test suite size | — | `python -m pytest -q` → **405 passed** (re-run this session, matches spec §2) | VERIFIED |
| 12 | No `projects`/`personal_root`/`module_context`/`vault_guard`/`digest`/`reflect` keys anywhere in current config schema | `singularity/config.py` (grep, no matches) | n/a — absence confirmed | VERIFIED (absence) |
| 13 | No `mb-capture`, `mb-onboard`, `mb-handoff`, `mb-triage` skills in current upstream | `singularity/skills/*` listing: compaction-plan, confidence-calibration, council, error-reports, grill, mb-llm-delegate, mb-memory, mb-search, memory-curator, observation-council, planning-council, report, workflow-helper | n/a — absence confirmed | VERIFIED (absence) |

Rows 1–11 reproduce and re-confirm spec §2 "Confirmed by the current code and tests" against
the current working tree (not just the prior session's notes). Rows 12–13 are new evidence
gathered in this task: they establish the negative baseline that T03–T15 build on top of.

## 3. Fork-only capability matrix (documentary contract, unverifiable by code)

None of these exist in the current upstream (§2, rows 12–13). Every row's only source is
the fork documentation already fully incorporated into
[[Singularity Incoming Fork Integration Specification]] §4/§6. No independent test run,
commit diff, or measured outcome (e.g. the fork's claimed `110 passed`) can be checked.

| Feature | Claimed by fork docs | Current upstream equivalent | Verified? | Owning future task |
|---|---|---|---|---|
| Multi-project registry + cwd detection | `projects: {id: {path, group, about}}`, deepest-path-wins resolver | none | documentary only | T03 |
| Personal space | notes outside `code/` = personal (fork rule) | none (rejected rule — see spec §5.1) | documentary only, and **rule itself rejected** | T03/T05 |
| Capture `--head` / bounded dedup | last-10-section similarity/prefix/gist dedup | `mb learn`/`mb note new` exist, no dedup/`--head` | documentary only | T05 |
| Digest planner/journal/nightly | `mb digest plan/mark`, `mb nightly` | none | documentary only | T11/T12 |
| Semantic lint (cross/same-file duplicate + supersession) | curator extension, report-only | curator exists, no semantic lint | documentary only | T13 |
| Project brain in SessionStart | per-project `.mb/context.json`, budgeted brain-note injection | SessionStart exists but has no project/brain concept (§2 row 7) | documentary only | T06 |
| Module context (once-per-session PreToolUse) | longest-prefix module note injection, marker-gated | no PreToolUse hook of any kind currently registered | documentary only | T07 |
| Session-scoped stop capture (code-edit based) | gate on `code_edits > 0` this session, not tool-count | current gate is `tool_uses >= 8` substring heuristic (§2 row 5) — **contradicts** fork claim, current behavior is the verified one | current behavior VERIFIED; fork replacement documentary only | T09/T10 |
| Vault guard | deny/audit mode for out-of-band note/config writes | none | documentary only | T08 |
| Config snapshot (`config.example.json`) | sanitized, git-trackable registry snapshot distinct from `mb backup` | `mb backup` exists; no sanitized example generation | documentary only | T08 |
| Reflect telemetry | offline adoption metrics from transcripts | none | documentary only | T14 |
| Workflow skills (`mb-capture`, `mb-onboard`, `mb-handoff`, `mb-triage`) | ported Claude skills | absent from current skill set (§2 row 13); `compaction-plan` exists and already implements a related but distinct one-shot handoff contract | documentary only; `compaction-plan` is prior art, not the same skill | T15 |
| Fork's own measured outcome (`110 passed`, specific edge cases) | claimed in fork docs | not independently reproducible — source absent | **OPEN, cannot be closed without fork source** | none — permanent gap, tasks must not cite it as evidence |

## 4. OPEN gaps (explicit, per T00 acceptance)

1. Fork source/commits are confirmed unavailable (§1) — this is final for the current
   environment, not a "try again later" gap.
2. The fork's own test results and exact edge-case behavior cannot be independently
   verified and **must never be cited as evidence** in T01–T16; only this document's
   §2 (code-verified) and §3 (documentary-only, explicitly labeled) may be cited.
3. **RESOLVED 2026-07-12 (T09/T10).** Row "session-scoped stop capture" in §3 was a
   direct behavioral contradiction between current upstream (tool-count) and the fork's
   documented replacement (code-edit count). `singularity/session_activity.py` (T09)
   replaced the tool-count/substring model with normalized `SessionActivity` +
   `decide()`; `singularity/hooks/stop_capture.py` and `gemini_after_agent.py` (T10)
   were rewired to parse the REAL transcript JSONL shape (verified against a live
   Claude Code transcript, not assumed) and share the same decision core. The old
   `MIN_TOOL_USES`/`should_remind` tool-count criterion no longer exists in code.
4. `compaction-plan` skill is pre-existing prior art adjacent to fork's `mb-handoff`;
   T15 must reconcile rather than duplicate (spec §6.6 already assumes this but it is
   restated here as a concrete gap for the implementing task).

## 5. Acceptance check

Per the T00 acceptance criterion ("no later task may refer to «the way the fork did it»
without a file/test reference or an explicitly recorded documentary contract"): every fork-only row
in §3 is now explicitly labeled *documentary only* with its owning task, and every
current-upstream claim in §2 carries a re-verified `file:line` plus this session's test
run. T01–T16 AI prompts may cite this document instead of re-deriving evidence.
