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

- **Since v0.9.0 the config is a MACHINE file: `~/.singularity.json`.** It is
  no longer inside the vault, because a vault travels between machines and
  every absolute path in it is wrong on arrival. The first `mb` command after
  the upgrade adopts your old `<vault>/.mb/config.json` automatically and
  renames it to `config.json.migrated-<ts>` — kept, never deleted. Nothing to
  run, and a machine that already has its own config is never overwritten by an
  arriving vault. An unparseable legacy file is a loud refusal, not a silent
  fresh start.
  **If a daemon is running during that first command, restart it**
  (`mb daemon restart`): a daemon started before the upgrade holds the old
  module, keeps reading the renamed file, and `mb daemon reload` will report
  nothing changed. The migration says so when it sees one.
- **The config records decisions, not defaults.** `Config.save()` writes only
  values that differ from `DEFAULT_CONFIG` (a real machine's file went from 42
  materialised keys to 9). An absent key therefore means "the shipped default",
  which is what keeps the file readable as the schema grows. Practical
  consequence when a default CHANGES in a later release: a value you set
  explicitly that happened to equal the old default is not stored, so you would
  move to the new default with everyone else. If you want a value pinned
  against future changes, set it to something other than the current default,
  or re-set it after the release that changes it. Values that differ from the
  default — including every deliberate opt-out — survive untouched.
- **Nothing new starts intercepting an existing session.** A changed default
  reaches a machine only where that machine never expressed a choice;
  `auto_capture` and friends keep whatever you set.
- **`schema_version` migrations still take a copy.** A timestamped
  `.singularity.json.bak-<ts>` is written before any migrating rewrite
  (`config.migrate_config()` / `config._backup_config()`) — separate from the
  one-time `.migrated-<ts>` above, which preserves the vault-era file.
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
- **A config still sitting in the vault** — adopted on the next `mb` command
  (see above). `doctor A7-config-location` reports one that is still there,
  which after the adoption means something wrote a new config back into a
  folder that travels.
- **Nothing stated for this machine yet** — since v0.9.1 the migration and the
  installer print what is unstated (`code_repos_dir`, `projects`, `llm`,
  `imgqa_ml_venv`) with the exact command for each, and `doctor
  A8-machine-setup` reports the same list. Every one of them is optional: an
  arriving vault carries the settings and none of the local answers, so this is
  a prompt at the two moments it is actionable rather than a standing warning.

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
- **The config gains 11 keys with safe defaults** and `schema_version`
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

## Machine-specific paths are gone from the source (v0.9.0)

An earlier release demoted the original dev machine's literals to "the last
fallback". v0.9.0 removed them instead. A default nobody can satisfy is not a
fallback — it outlived its own machine's move to another drive and shipped a
dead drive letter to everyone else.

- **No built-in vault names a drive.** Resolution order is unchanged
  (`--vault` → `MB_HOME` → `~/.singularity.json` → built-in candidates), but
  the candidates are home-relative only: `~/memory brain`, then
  `~/Documents/memory brain`, each tried only if it exists. Nothing configured
  now yields "no vault configured" with actionable guidance instead of a path
  from someone else's machine. Set **`MB_DEFAULT_VAULT`** (one path, or several
  separated by the platform's path separator) to replace that list entirely.
  `MB_HOME` says *which vault to use*; `MB_DEFAULT_VAULT` says *where to look
  when nothing was configured*.
- **The installer ASKS.** `--vault` wins and creates the directory; otherwise
  the resolved path is shown for confirmation rather than assumed. A
  non-interactive run never prompts — it fails naming the flag instead of
  hanging on a question nobody can answer.
- **Agent and skill text carries a `{{VAULT}}` token**, substituted at install
  time. The literal path used to be repeated in 18 files with a matching
  sentinel in `register_agents`, a pairing that silently stopped matching when
  the authoring machine changed drives. Files whose tree contains the token are
  rendered as owned copies (marked with `.singularity-owned.json`); everything
  else stays symlinked, so editing the repo still updates every deployed skill
  at once. A local file you edited yourself is backed up, never overwritten.
- **Two more machine literals went with them** (v0.9.0, found by the baseline's
  own scan): `imgqa`'s ML venv is named through `IMGQA_ML_VENV` or
  `imgqa_ml_venv`, and `mb llm setup` requires `--dir` the first time rather
  than downloading gigabytes onto a drive that need not exist. Both now say
  "not configured" instead of "not found at <a path you never chose>".

Nothing to run for the candidate list; token substitution happens on the next
installer / `mb doctor --fix` run.

## The command window (no migration required)

`mb --help` now lists ten commands instead of twenty-three. Nothing was
renamed, moved or removed — the window is a **view**, and each claim below is
covered by `tests/test_cli_facade.py` and by the full suite (1909 passed) run
against it.

- **Everything you already type works byte-for-byte.** `mb daemon status`,
  `mb curator gaps`, `mb config set …`, `mb project add …` — unchanged. The
  facade rewrites the *new* paths (`mb service daemon status`) onto the real
  commands before argparse sees them, so a grouped form and a direct form are
  one code path rather than two implementations that could drift. No skill,
  hook, scheduled job or script was edited for this release, and none needs to
  be: the direct paths remain the right thing for automation.
- **The additions are only additions.** `mb service …`, `mb setup …`,
  `mb curator nightly`, `mb status stats`, `mb help --all`, and a bare `mb`
  (one line of state) were all *errors* before this release, so no existing
  invocation changes meaning. `curator` and `status` keep printing exactly what
  they printed — they merely also answer for a sibling now.
- **One behavioural change, and it is in the exit codes.** A bare `mb` and
  `mb help` exit **0** instead of **2**; neither is an error any more. Only
  relevant if you have a script leaning on `mb` failing when called with no
  arguments (`mb || echo "broken install"`) — use `mb doctor` or `mb status`
  for that, which still exit non-zero when something is actually wrong. Every
  other exit code is untouched, including `mb <unknown command>` → 2 and
  a bare `mb` with no vault configured → 2.

Nothing to run beyond the usual `git pull` + installer. The next time you type
`mb --help` it looks different; `mb help --all` prints the full list, grouped,
with every direct path spelled out.

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
