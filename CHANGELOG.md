# Changelog

This file summarizes the `SINGULARITY_INCOMING_FORK` epic (T00–T16): porting
a user-authored fork's product contracts into current upstream Singularity.
The fork's own source was confirmed unavailable (T00) — everything below was
authored fresh against the current codebase from the fork's *behavioral*
specification, not ported line-for-line. Full task-by-task detail, decisions,
and real-machine verification evidence live in the project vault; this file
is the repo-facing summary. Commit hashes refer to `master`.

## R1 — Hook control MVP (T00–T02)

- `mb hooks status|enable|disable`, natural-language capture-hook mapping,
  `mb doctor`/`mb status` integration. (`5d5443f`, `69d2f6b`)
- Evidence baseline documenting what current upstream already had verified
  vs. what was fork-only/documentary. (`933cd92`, `docs/incoming-fork-baseline.md`)

## R2 — Multi-project foundation (T03–T05)

- Project registry (`mb project add|remove|list|detect`), deepest-path-wins
  cwd detection, reserved ids `general`/`personal`. (`9caab4e`)
- Project-aware index and search filters. (`92bbebf`)
- Project-bound capture, `mb learn --head`, bounded last-10-section dedup.
  (`a6af2c2`)

## R3 — Context and discipline (T06–T10)

- Project brain injected into SessionStart from `.mb/context.json`, budgeted
  excerpt (`brain_max_lines`). (`a975672`)
- Once-per-session module-context PreToolUse hook, opt-in
  (`module_context_enabled`). (`6d4813e`, `7027510`)
- Hook ownership detection fixed to recognize pre-marker legacy entries by
  command path, not just the `_mb` marker (real bug found registering the
  first PreToolUse hook on a live machine). (`0867045`)
- Vault guard PreToolUse hook (audit/deny modes, opt-in) + sanitized
  `config.example.json` snapshot generation. (`dddcf09`)
- `personal_roots` retrofit: the fork's blanket "everything outside `code/`
  is personal" rule was rejected; a narrower, explicit opt-in
  (`personal_roots` config + frontmatter) shipped instead. (`7267635`)
- Normalized `SessionActivity` + pure `decide()` core replacing the old raw
  tool-count/substring capture heuristic. (`37d695e`)
- Claude/Gemini capture adapters rewired onto the new core, parsed against
  REAL transcripts (not assumed) — found live that Windows Claude Code fires
  both `Bash` and `PowerShell` as distinct tool names. (`8c353f2`)

## R4 — Knowledge compilation (T11–T13)

- Digest planner: `mb digest plan|mark`, report-only merge candidates
  (learnings → reference/rule/howto/decision), atomic mark journal.
  (`1689b50`)
- **Bug found and fixed** (real-machine, not synthetic): `_writer_lock()`'s
  cleanup ran even on the losing/contended path, letting a losing writer
  delete the winning writer's active lock. (`18f0be6`)
- `mb nightly` orchestration: index update → digest/curator/config snapshot
  → trend line → markers sweep; retargets the old standalone
  `nightly-index` scheduled job; SessionStart digest-nag (threshold + 72h
  rate limit); `watch.py`'s pending-file-loss-on-lock-contention bug fixed.
  (`2c7161c`)
- Committed-index loader promoted to a shared `indexer.load_committed_index()`
  (was duplicated between `digest.py` and about to be duplicated again).
  (`477ef76`)
- Curator semantic lint: chunk-level near-duplicate detection (blockwise
  cosine, bounded memory), supersession candidates, `.mb/context.json`
  budget/reference checks. **Bug found and fixed**: `curator._note_embeddings()`
  read a stale legacy flat index path instead of the current committed
  generation — confirmed ~2 days stale on the live vault. (`ea6fe24`)

## R5 — Client-neutral workflow skills (T15)

- `mb-onboard`, `mb-triage` added as new skills; the fork's `mb-capture`/
  `mb-handoff` contracts turned out to already exist as `mb-memory`/
  `compaction-plan` — extended in place rather than duplicated.
  `install/skill_manifest.py`: single source for which client gets which
  skill, replacing two independently-maintained lists. Verified against the
  real installed Claude/Codex/Gemini clients on the development machine.
  (`9e97e63`)

## R6 — Telemetry and release gate (T14, T16)

- `mb reflect`: privacy-safe adoption telemetry — offline transcript walk,
  counts/rates only, no prompt/tool-argument/session-id/transcript-path
  persistence ever. Transcript locations and wire formats (including
  subagent exclusion via `isSidechain`) verified against real Claude Code
  and Gemini CLI session data, not documentation. (`c7b9119`)
- Release gate (this task): fixture-matrix E2E coverage (legacy config,
  malformed settings, foreign hooks, full install→status→disable→enable→
  doctor→uninstall lifecycle, SessionStart-independent-of-`auto_capture`
  proof, general-classification/no-file-move proof), measured performance
  numbers, a cross-cutting privacy audit, and this documentation pass
  (README, [UPGRADE.md](docs/UPGRADE.md), [ROLLBACK.md](docs/ROLLBACK.md)).

## Known open items (not silently claimed as done)

- Codex has an install phase (`AGENTS.md` profile + skill junctions) but no
  `mb doctor` health check and no runtime hook, unlike Gemini — see
  README's "Other CLI clients" section.
- `mb reflect`'s `eligible_research`/`code_edits` classification for Gemini
  sessions is cruder than Claude's (no per-event cwd boundary in the
  verified transcript shape) — documented as a limitation in
  `singularity/reflect.py`, not silently assumed equivalent.
