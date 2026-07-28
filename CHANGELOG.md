# Changelog

This file summarizes the `SINGULARITY_INCOMING_FORK` epic (T00–T16): porting
a user-authored fork's product contracts into current upstream Singularity.
The fork's own source was confirmed unavailable (T00) — everything below was
authored fresh against the current codebase from the fork's *behavioral*
specification, not ported line-for-line. Full task-by-task detail, decisions,
and real-machine verification evidence live in the project vault; this file
is the repo-facing summary. Commit hashes refer to `master`.

---

# v0.5.0 — CODE-INDEX: the third corpus (C00–C07)

Closes **MB-05** (`TASK-05_Code_Indexer`, open since 2026-07-02): semantic
search over source repositories, alongside memory. Ships in the same release as
WIKI-LOOP. Full spec, decisions (`decisions/CODE-INDEX D1…D6`) and
real-machine evidence live in the vault
(`03_Tasks/SINGULARITY_CODE_INDEX_IMPLEMENTATION.md`); the user-facing guide is
[`docs/code-index.md`](docs/code-index.md).

The original 2026-07-02 spec wanted a standalone `search/code_indexer.py` with
its own scanning, mtime increment and npy+json format. WIKI-LOOP's `Corpus`
already provides all of that, so this epic **extended the shared writer instead
of duplicating it** — and in doing so surfaced two seams the spec never had.

## What shipped

- **`Corpus` is genuinely corpus-agnostic** (C01): `patterns`, `prune_dirs`,
  `skip_file_patterns`, `max_file_bytes` and a per-corpus `chunk_schema`.
  `_scan_corpus` moved from `rglob` to `os.walk` so `node_modules` is *pruned
  during traversal* rather than walked and discarded. Filter order is now a
  tested contract — directory → filename → path → `stat()` — because the real
  root offers ~111 extension-matching candidates per file actually wanted.
- **Defect fixed: `chunk_schema` was global** (D3). `_commit_generation`
  stamped `chunking.CHUNK_SCHEMA_VERSION` onto *every* corpus and `_update`
  compared against it, so bumping the markdown chunker forced a pointless full
  rebuild of the code index — and bumping the code chunker forced nothing. It
  is now a required keyword-only argument; no writer can inherit a format it
  did not produce. **Migration is a no-op by construction**: every generation on
  disk already carries the old global number, which is exactly what memory and
  sources now declare.
- **`search/code_chunking.py`** (C02): regex symbol boundaries, Python-first
  (69% of the codebase, and the one language Genevra had no chunker for) —
  methods as their own chunks, decorators pulled in, docstrings read forward
  from the signature. TS/JS/JSX ported. Ruby deliberately *not* implemented
  (D6: zero `.rb` files exist; it falls back rather than pretending).
- **`code_corpus` + `mb index rebuild|update|inspect --code`** (C03). Unset
  `code_repos_dir` is the off switch and every entry point refuses with an
  actionable message instead of writing an empty index.
- **`mb search --with-code` and `--repos`** (C04), plumbed and tested at every
  link including the cold fallback. `--code-only` stopped being a stub. Code is
  opt-in (D2), so the default result set is provably unchanged.
- **`mb nightly` step `code_index`, plus `mb stats` / `mb status` / `mb doctor`
  block `K1`** (C05). Doctor never FAILs over code — the corpus is fully
  derived, unlike `.mb/sources/` — and `mb doctor --fix --heavy` builds it.
- New config key `code_index_max_age_days` (default 7); `code_repos_dir` gained
  a `~`-expanding accessor.

## Evidence

- Live vault, C01 gate: `mb index update` after the upgrade did **not** rebuild
  (`added: 0`), `memory_index/CURRENT` unchanged, `mb curator --json` and
  `mb digest plan --json` byte-identical by SHA-256.
- Real root `D:\Project`: 13 repos, **416 files → 5775 chunks**, 8.9 MB
  embeddings, 68 s cold build, 0.5 s no-op update, warm search 15.3 ms
  (default) vs 16.8 ms (`--with-code`). Fallback share **1.6%**.
- Privacy (D5 layer three — the only layer that checks a fact): grep of the
  built `metadata.json` for `AKIA`, PEM headers, `password=`, `api_key`,
  `token=`, `Bearer`, `sk-`, `ghp_` → **0 hits**; zero `.env`/`.pem`/dump files
  indexed.
- D2 by the numbers: `--with-code` took 10 of 50 result slots and displaced
  4 notes; **top-1 changed on 0 of 10 queries**. `mb search` without flags
  matched the pre-feature baseline on all 10 queries.
- Tests **1492 → 1605**.

---

# v0.5.0 — WIKI-LOOP: raw sources, ingest fan-out, intel lint (W00–W12)

Closes the gap with the LLM-wiki method: an external document is stored
immutably with provenance, and its knowledge is deliberately spread across the
existing pages it actually touches. Full spec, decisions and real-machine
evidence live in the vault (`03_Tasks/SINGULARITY_WIKI_LOOP_IMPLEMENTATION.md`,
`decisions/WIKI-LOOP D1…D7`); the user-facing guide is
[`docs/wiki-loop.md`](docs/wiki-loop.md).

## R1 — the source layer (W00–W04)

- **Decisions D1–D7 recorded before any code** (owner-signed): raw lives in
  `.mb/sources/` outside the vault tree, indexed as its own corpus, no network
  in the engine, semantics in skills, hard-deny immutability, fan-out ceiling 15.
- `singularity/sources.py` — content-addressed ids, atomic registry, write-once
  storage, Windows-safe slugs, tolerant read / strict write.
- `mb ingest add|list|show|export` with a contract exit-code scheme, zero-side-effect
  `--dry-run`, and an explicit refusal (plus the working command) for `http(s)` URLs.
- **`Corpus` abstraction in the indexer**: scanning, chunking, committing and
  locking are parameterized by (root, index_dir) instead of hardcoding the vault.
  Writer locks became per-corpus, so indexing sources never blocks the watcher.
  `mb search --with-sources` reaches the raw layer; nothing reaches it without
  the flag.
- Provenance contract: `sources:` frontmatter, `mb note new --from-source`,
  two new non-fatal `mb curator` checks, `immutable_roots` denied even in the
  guard's `audit` mode.

## R2 — the working cycle (W05, W06, W09)

- `mb ingest plan` — deterministic fan-out with mandatory `why` evidence per
  proposed page, `new_pages`, cross-corpus `conflicts`, and a refusal (rather
  than nonsense) when the two corpora were built by different models.
- Shared vector math extracted to `search/similarity.py`; proven bit-identical
  to the previous inline implementations on the live 3083-chunk index.
- `mb ingest mark` / `mb ingest log` + `.mb/ingest-log.jsonl`. A handled page
  stops resurfacing; a re-ingested (re-hashed) document brings it back.
- Skills: new `mb-ingest` (9 gated steps), `memory-curator` extended with
  adjudication and the research agenda.

## R3 — intelligence (W07, W08)

- `mb curator contradictions` — cosine band (0.72, 0.90) plus five lexical
  divergence signals, each producing evidence tokens; verdict journal so a
  dismissed false positive never returns. **Tuned on real data**: the first
  run produced 8762 candidates in 27.5 s; requiring an asserting signal,
  keying numeric mismatches, excluding file:line citations and caching
  per-chunk features brought it to 653 candidates in 1.4 s with no cap.
- `mb curator gaps` — six deterministic signals answering "what is missing",
  with true totals plus a per-signal ceiling so one broad signal cannot bury
  the rest.

## R4 — migrations, integration, release (W10–W12)

- `mb ingest backfill` — idempotent retro-provenance from `source:` frontmatter
  and URLs in `reference/` bodies.
- `schema_version` 1 → 2: 11 additive keys, no value rewriting, `.bak` before
  the migrating save.
- **`mb backup` now covers the ingest layer** — registry and every raw file with
  sha256; `restore` is strictly additive and never overwrites. This is the
  compensation the D1 storage decision owed: `.mb/sources/` is the only
  non-reconstructible data in the vault.
- `mb doctor` block W (FAIL when the registry outlives its files; `--fix` never
  deletes raw), nightly steps + trend fields, SessionStart ingest nag, sources
  sections in `mb status` / `mb stats`.
- E2E on a clean vault, and an R8 regression proving `mb uninstall`,
  `mb doctor --fix`, the documented rollback and a full reindex all leave every
  raw file byte-identical.

### Also fixed on the way

- `encoder.resolve_device()` now probes CUDA with a real kernel launch and falls
  back to CPU. `torch.cuda.is_available()` returning True on a GPU the installed
  torch has no kernels for took the whole search daemon down; the config key
  `encoder_device` overrides the probe.
- `sentence-transformers` pinned `<5` — the 5.x line imports `torchcodec`
  unconditionally, which needs FFmpeg shared libraries this project has no use for.

---

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
