# Upgrade guide

How an existing Singularity install picks up everything shipped across the
`SINGULARITY_INCOMING_FORK` epic (multi-project registry, personal space,
capture dedup, digest planner, project brain, module context, vault guard,
config snapshot, nightly orchestration, curator semantic lint, client-neutral
skills, adoption telemetry) safely — without losing memory, foreign hooks, or
other client automation.

## TL;DR

```bash
git pull
./install/install.ps1     # Windows, or install.sh on macOS/Linux
mb doctor --fix
```

Re-running the installer is always safe: it refreshes and re-verifies
instead of breaking things (see `install/register_*.py` docstrings — every
one of them is idempotent by construction). No step below is destructive,
and nothing here requires you to do anything by hand for a normal upgrade.

## What actually changes on your machine

- **`.mb/config.json` gains new keys with safe defaults**, written the next
  time anything calls `Config.save()` (installer re-run, `mb config set`,
  `mb init`). `schema_version` bumps and a timestamped backup
  (`config.json.bak-<ts>`) is written automatically before any migrating
  rewrite (`config.migrate_config()` / `config._backup_config()`) — your
  previous config is never overwritten without a copy.
- **Nothing new starts intercepting an existing session.** `Config.save()`
  materializes every default into `config.json`, so a machine that has ever
  saved its config keeps the values it already has — a changed default reaches
  new installs only. `auto_capture` likewise keeps whatever value it had.
- **`vault_guard_enabled` ships `true` (audit) since v0.6.3**, `module_context_enabled`
  still `false`. If your config predates that and you WANT the guard, set it:
  `mb config set vault_guard_enabled true`. Audit only annotates a write with a
  hint to use `mb note new` / `mb learn`; `deny` remains explicit opt-in.
- **`.mb/` gains new subdirectories/files on first use, not on upgrade
  itself**: `.mb/markers/` (per-session nudge markers), `.mb/digest.json`
  (mark journal), `.mb/digest-last.json` / `.mb/curator-last.json` /
  `.mb/config-last.json` / `.mb/nightly-history.jsonl` (written by the first
  `mb nightly` run, whether scheduled or manual), `.mb/reflect-last.json` /
  `.mb/reflect-history.jsonl` (written by the first `mb reflect` run). None
  of these exist until their owning command runs at least once.
- **The embedding model gets an explicit local manifest.** Re-running the
  installer adopts a complete snapshot already present in `.mb/hf-cache`
  without downloading it again (or downloads it once when genuinely absent),
  writes `.mb/hf-cache/singularity-model.json`, restarts the daemon, and
  rebuilds through that warm process. If you update without re-running the
  installer, `mb doctor` reports `B3-model`; repair with
  `mb doctor --fix --heavy` or `mb model install`. Normal search/index
  runtime is offline-only and cannot silently contact Hugging Face.
- **Existing notes are never moved or rewritten.** No code path in
  `singularity/search/` or `singularity/curator.py` calls `shutil.move`,
  `Path.rename`, or `os.rename` on a vault file — confirmed by direct source
  grep and a regression test (`tests/test_release_gate.py::
  test_no_move_or_rename_call_exists_anywhere_in_the_indexing_path`). A note
  with no `project:` frontmatter classifies as **general** at query/index
  time; it is never physically relocated into a project folder or a
  fork-style `code/` directory.
- **The nightly scheduled job is retargeted, not duplicated.** The old
  standalone `nightly-index` job (`index update` only) is unregistered and
  replaced by a single `nightly` job (`mb nightly`'s full chain) the first
  time `mb doctor --fix` or the installer runs post-upgrade — see
  `singularity/nightly.py::migrate_scheduler_job()`. If you never run
  `--fix` or re-install, the old job keeps running exactly as before; you
  just won't get the newer chain (digest/curator/config snapshots, trend
  line, markers sweep) until you do.
- **Claude's skill list grows from 13 to 15** (`mb-onboard`, `mb-triage`
  added; `mb-memory` and `compaction-plan` gained new capabilities in
  place — see [Rollback](ROLLBACK.md) if you want the old, narrower
  behavior). Codex/Gemini's portable skill set grows from 10 to 12 the same
  way, computed from `install/skill_manifest.py` on the next install run.

## Legacy configs specifically

- **No `projects` key at all** — fully supported, `cfg.get("projects", {})`
  defaults to `{}` everywhere it's read; `mb project list` just shows
  nothing registered yet. Covered by
  `tests/test_release_gate.py::test_legacy_config_without_projects_key_*`.
- **No `personal_roots` key** — same story, defaults to `[]`; nothing is
  classified `personal` unless you explicitly set `--project personal` or
  configure a root.
- **A config from before `schema_version` existed** — `load_config()`
  detects the missing/old version, migrates in memory, backs up the old
  file, then saves — this already happened for every machine that upgraded
  through AUD-13 and needs no new action now.

## Grok Build CLI support (feat/grok-cli-integration)

Upgrading onto a machine that has the xAI Grok Build CLI adds a fourth
client on the next install / `mb doctor --fix` run:

- **Prerequisite** (yours to install; Singularity never installs,
  updates or authenticates Grok): the official CLI — see the README's
  "Other CLI clients" section for the install one-liners. Minimum proven
  version **0.2.111**; below it the profile and skills still deploy but the
  runtime capture hook is skipped (`F4-grok-capture: SKIP`) because the
  stop-gate contract is unproven there.
- **Native paths**: everything lives under `$GROK_HOME` when set, else
  `~/.grok` — the same resolution order the CLI itself uses; install,
  doctor and uninstall all share that one resolver.
- **What appears**: the managed `SINGULARITY:BEGIN/END` block in
  `<grok-home>/AGENTS.md`, 13 portable skill junctions in
  `<grok-home>/skills/`, and the owned Stop-capture hook file
  `<grok-home>/hooks/singularity.json`.
- **Safe re-registration**: re-running the installer (or
  `mb doctor --fix`) refreshes only the managed block and our links —
  byte-identical reruns are no-ops, and a foreign file squatting our hook
  filename is backed up (`singularity.json.bak-<ts>`), never overwritten
  silently.
- **Capability tier is observed, not promised**: capture nudge yes
  (block-mode, once per session), dynamic session-start recall no (Grok has
  no context-injection channel — the AGENTS.md block carries a static
  recall instruction instead), `mb reflect` counts Grok sessions.
  See `tests/fixtures/grok/README.md` for the full evidence matrix.

## WIKI-LOOP: the raw source layer (`schema_version` 1 → 2)

The ingest epic (`W00`–`W12`) adds a layer that stores external documents with
provenance. Upgrading to it is additive; the one thing you genuinely must read
is the backup note.

- **The main vault index is NOT rebuilt.** `CHUNK_SCHEMA_VERSION` stays at 3
  because raw sources live in their own corpus (decision D2), so `mb index
  update` after the upgrade does exactly what it did before — no multi-minute
  re-embed of your whole vault. Verified on a 3083-chunk vault: the
  `memory_index` `CURRENT` generation pointer was byte-identical before and
  after ingesting a document.
- **One new index gets built on demand**: `.mb/sources_index/`, by
  `mb ingest index --rebuild`. It costs proportionally to how much you have
  ingested, and it is empty until you ingest something (measured: 1 document /
  18 chunks in 0.34 s).
- **`.mb/config.json` gains 11 keys with safe defaults** and `schema_version`
  bumps 1 → 2. Nothing is rewritten: the new keys resolve through
  `DEFAULT_CONFIG`, so a v1 config keeps every value you set and gains only
  defaults. A timestamped `config.json.bak-<ts>` is taken before the migrating
  save, as always.
- **New files appear only when their command first runs**: `.mb/sources.json`
  (registry), `.mb/sources/<YYYY>/*.md` (raw copies), `.mb/ingest-log.jsonl`
  (cycle history), `.mb/lint.json` (contradiction verdicts),
  `.mb/rebuild-sources.lock` (per-corpus writer lock).
- **`mb curator` output gains two non-fatal issue keys**
  (`source_not_in_registry`, `orphan_source`) and two optional subcommands
  (`contradictions`, `gaps`). Bare `mb curator` behaves exactly as before —
  the exit code still depends on `broken_links` alone.
- **Retro-provenance is opt-in**: `mb ingest backfill` (dry run by default,
  `--apply` to write) registers the external documents your existing notes
  already cite in `source:` frontmatter and `reference/` bodies, as
  `stored: false` records. Without it `mb curator gaps` believes the vault has
  no sources at all.

### ⚠️ `.mb/sources/` is the only thing here a rebuild cannot recreate

Everything else under `.mb/` is derived state — delete it and the next index
run restores it. Raw sources are not: they are the evidence your notes cite,
and the copy in `.mb/sources/` is the only one. Consequences you should act on:

- **`mb backup create` now covers them** (registry + every raw file, with
  sha256 in the manifest). Large collections are skipped with a warning past
  `backup_max_sources_mb` (default 200 MB) — force with
  `mb backup create --with-sources`, or evacuate once with
  `mb ingest export <dir>`.
- **`mb backup restore` is additive**: it adds missing raw files and merges
  missing registry entries, and never overwrites or deletes what is on disk.
- **If `.mb` is in `.gitignore` or excluded from your sync**, moving the vault
  to another machine leaves the sources behind. `mb doctor` warns when the
  vault is a git repo and `.mb/sources` is ignored, but the fix is yours:
  either un-ignore `.mb/sources/`, or keep an `mb ingest export` copy
  somewhere that is synced.
- **`mb ingest add` is write-once**: an existing stored file is never
  rewritten, on any code path. Re-ingesting a changed document creates a new
  record linked by `supersedes`/`superseded_by` instead.

## CODE-INDEX: the code corpus (no migration required)

The code epic (`C00`–`C07`) adds a third search corpus. **Nothing on your
machine needs migrating, and this is true by construction rather than by
luck** — the three claims below are each backed by a test and by a run on a
live 3216-chunk vault.

- **Your vault index is NOT rebuilt.** `chunk_schema` became per-corpus
  (decision D3, fixing a real defect — see the CHANGELOG). Every generation
  already on disk carries the old *global* number, and that is exactly what the
  memory and sources corpora now declare, so the first `mb index update` after
  the upgrade is an ordinary no-op diff. Verified on a live vault: `added: 0`,
  `memory_index/CURRENT` unchanged, `mb curator --json` and `mb digest plan
  --json` byte-identical by SHA-256. Regression:
  `tests/test_indexer.py::test_a_generation_stamped_by_the_old_global_schema_does_not_rebuild`.
- **The corpus is off until you switch it on.** No `code_repos_dir`, no code
  corpus — that unset key IS the switch (D1), and there is deliberately no
  second boolean that could disagree with it. `mb nightly` reports the step as
  `skipped` with a reason, `mb doctor` as `SKIP`, and `mb stats`/`mb status`
  say nothing at all.
- **`schema_version` does NOT bump.** `code_repos_dir` already shipped in
  `DEFAULT_CONFIG` before this epic. Two genuinely new keys arrive —
  `code_index_max_age_days` (default 7, when `mb doctor` starts warning that the
  corpus is stale) and `code_skip_dirs` (default `[]`, extra directory names
  pruned at any depth) — and both resolve through `DEFAULT_CONFIG` like every
  other additive key, so a config you never touch keeps every value it has.
  `migrate_config()` is untouched.

### If you DO want it

```powershell
mb config set code_repos_dir "<path/to/your/repos>"   # the PARENT of your repos
mb index rebuild --code                               # or: mb doctor --fix --heavy
```

The first build loads the encoder and takes minutes on a large root (measured:
440 files → 3666 chunks in 71 s including daemon start). After that, `mb
nightly` keeps it current — there is no watcher over repositories by design
(D4), which is also why `mb doctor` WARNs once the corpus is older than
`code_index_max_age_days`.

`.mb/code_index/` costs roughly 30 MB for that corpus (two generations kept:
current + last-good). It is **fully derived state** — deleting it loses
nothing, which is why no `mb doctor` scenario FAILs because of it.

### One case worth knowing about

If you had already set `code_repos_dir` speculatively before upgrading, the
nightly `code_index` step will start building that corpus on its next run.
That is the feature working as intended, but it is the one way this upgrade
does something you did not explicitly ask for after the fact. To opt back out:
`mb config set code_repos_dir null`.

## Machine-specific paths became defaults, not requirements

Two literals belonging to the original dev machine were baked into the source.
Both still work exactly as before — they are now the LAST fallback rather than
the only possibility, so nothing changes on a machine that was already working.

- **The built-in fallback vault.** Resolution order is unchanged (`--vault` →
  `MB_HOME` → `~/.singularity.json` → built-in), but the last step is now a
  LIST of candidates: `~/memory brain`, `~/Documents/memory brain`, then the
  historic `D:\obsidian\memory brain`. Each is still tried only if it exists.
  Set **`MB_DEFAULT_VAULT`** (one path, or several separated by the platform's
  path separator) to replace that list entirely. Note the difference from
  `MB_HOME`: `MB_HOME` says *which vault to use*, `MB_DEFAULT_VAULT` says
  *where to look when nothing was configured*.
- **The vault path inside the agent definitions.** `singularity/agents/*.md`
  tell the model to read rules out of the vault and name a concrete path, so
  they stay readable as documents. That literal is now a template default: if
  this machine's vault resolves elsewhere, `install/register_agents.py` writes
  a substituted copy instead of a symlink and says so. If the vault matches the
  template — the historic case — nothing changes and the files stay symlinked,
  which preserves the property that matters: edit the repo, every deployed
  agent updates at once. A local file you edited yourself is still backed up,
  never overwritten.

Nothing to run: the candidate list applies on the next command, and the agent
substitution on the next installer / `mb doctor --fix` run.

## Foreign hooks and settings

Every settings mutation (Claude `~/.claude/settings.json`, Gemini
`~/.gemini/settings.json`/`GEMINI.md`, Codex `~/.codex/AGENTS.md`, Grok
`<grok-home>/AGENTS.md`/`hooks/singularity.json`) is a
marker-delimited merge or an owned-entry filter — never a blind overwrite.
A hook/block that isn't ours (no `_mb`/named-hook marker, or outside the
`SINGULARITY:BEGIN/END` markers) survives every install, upgrade, and
uninstall untouched. Verified end-to-end in
`tests/test_release_gate.py::test_foreign_hooks_survive_full_lifecycle`
(install → status → disable capture → re-enable → doctor → uninstall, a
foreign Stop hook present throughout) and the pre-existing
`tests/test_cli_uninstall.py`.

## If something looks wrong after upgrading

`mb doctor` first — it diagnoses structure, deps, index, daemon, scheduler,
and hooks in one pass and tells you exactly what to run next. See also
[ROLLBACK.md](ROLLBACK.md) to turn any specific new mechanism back off
without reinstalling or losing data.
