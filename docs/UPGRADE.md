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
- **Every new opt-in mechanism defaults to OFF**: `module_context_enabled`,
  `vault_guard_enabled` both default `false`. `auto_capture` keeps whatever
  value it already had. Nothing new starts intercepting your session unless
  you explicitly turn it on.
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
  `<grok-home>/AGENTS.md`, 12 portable skill junctions in
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
