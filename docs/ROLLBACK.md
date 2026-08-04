# Rollback guide

How to turn any specific mechanism shipped in the `SINGULARITY_INCOMING_FORK`
epic back off, without a full uninstall and without losing vault data. Every
rollback below is a config flip, a scheduler/hook removal, or `mb uninstall`
— nothing here deletes a note or a journal file.

## Individual feature flags

All of these live in the machine config `~/.singularity.json` (since
v0.9.0 — it is not in the vault, because a vault travels between machines),
set with
`mb config set <key> <value>` (validated, atomic write) or by hand-editing
and running `mb config validate` afterward.

| Feature | Flag | Rollback |
|---|---|---|
| Capture nudge (Stop hook) | `auto_capture` | `mb hooks disable` (equivalent to `mb config set auto_capture false`). SessionStart is **not** affected — it has no dependency on this flag at all (structurally verified: `auto_capture` does not appear anywhere in `singularity/hooks/session_start.py`'s source, `tests/test_release_gate.py::test_sessionstart_never_reads_auto_capture_key_at_all`). |
| Module-context injection (PreToolUse) | `module_context_enabled` | `mb config set module_context_enabled false` — default is already `false`; only relevant if you turned it on |
| Vault guard (PreToolUse) | `vault_guard_enabled` / `vault_guard_mode` | `mb config set vault_guard_enabled false` — default already `false`. To keep it on but stop it from blocking, set `vault_guard_mode audit` (warn-only) instead of `deny` |
| Digest nag on SessionStart | `nightly_nag_rate_limit_hours` | Not a hard off-switch by design (it's a rate limit, not a toggle) — set a very large value (e.g. `87600` = 10 years) to effectively silence it, or stop running `mb nightly` (see below) so `.mb/digest-last.json` never refreshes |
| Nightly orchestration | — | `mb doctor --fix` only registers the job if it's missing/drifted; to stop it running at all, remove the scheduled task directly (Windows: `schtasks /Delete /TN Singularity-nightly`; macOS: unload `~/Library/LaunchAgents/com.singularity.nightly.plist`; Linux: `systemctl --user disable --now singularity-nightly.timer`) — `mb nightly` still works fine run manually |
| Code corpus (`--with-code`) | `code_repos_dir` | `mb config set code_repos_dir null` — see below; the unset key IS the off switch (D1) |

## Rolling back the code corpus

The easiest rollback in this repo, and deliberately so: `.mb/code_index/` is
**100% derived** from files that live in your repositories. Nothing there is
data — delete it and one command recreates it byte-for-byte equivalent.

```bash
mb config set code_repos_dir null        # corpus off: no nightly step, no search, no doctor block
rm -rf "<vault>/.mb/code_index"          # optional, reclaims ~30 MB per corpus (2 generations kept)
mb daemon stop && mb daemon start        # drop the loaded code embeddings from memory
```

What each step turns off:

- **`code_repos_dir null`** — `mb search --with-code` / `--code-only` refuse
  with exit 2, `mb index rebuild --code` refuses with exit 2, the nightly
  `code_index` step reports `skipped`, `mb doctor` reports `K1-code-index:
  SKIP`, and `mb stats` / `mb status` stop printing the code line. No other
  behavior changes anywhere.
- **Deleting `.mb/code_index/`** — reclaims disk. Safe at any time, including
  with the corpus still configured: the next `mb index update --code` sees no
  index and does a full rebuild.
- **Restarting the daemon** — the engine keeps the code embeddings resident
  until it reloads. Purely a memory concern; searches already stopped
  returning code the moment the config key went away.

### What rolling this back does NOT touch

- **Your vault index.** Separate directory, separate generation pointer,
  separate writer lock, separate `chunk_schema`. `memory_index/CURRENT` is not
  read or written by any code-corpus path — regression:
  `tests/test_code_index_e2e.py::test_building_code_changes_neither_the_memory_pointer_nor_its_analytics`.
- **`mb curator` / `mb digest` / `mb ingest plan`.** They never saw the code
  corpus in the first place; their output is byte-identical with it present,
  absent, or half-built.
- **Your source files.** The indexer only ever reads them. No code path in
  `singularity/search/` moves, renames or writes anything under
  `code_repos_dir`.

### Reverting `code_index_max_age_days`

`mb config set code_index_max_age_days 7` restores the default. This key only
controls when `mb doctor` starts WARNing about staleness; it has no effect on
indexing or search, and no value of it can make `mb doctor` FAIL.

## Rolling back the WIKI-LOOP ingest layer

```bash
mb config set ingest_index_sources false   # stop building/searching the sources corpus
rm -r "<vault>/.mb/sources_index"          # drop the derived index (rebuildable)
```

That is the whole rollback of the search side. Additional switches:

| Feature | Flag | Rollback |
|---|---|---|
| Sources corpus (second index) | `ingest_index_sources` | `mb config set ingest_index_sources false` — `mb ingest index` and `mb search --with-sources` then refuse with an actionable message instead of silently doing nothing |
| Raw immutability guard | `immutable_roots` | `mb config set immutable_roots []` — only meaningful while `vault_guard_enabled` is `true` (default `false`, i.e. already a no-op). The engine-level write-once check is NOT a flag and does not turn off |
| Contradiction/gap reports | — | Nothing to disable: `mb curator contradictions` / `mb curator gaps` are opt-in subcommands. Bare `mb curator` never runs them |
| LLM adjudication of lint | `lint_llm_adjudication` | Default `false`; `mb config set lint_llm_adjudication false` to be explicit |
| Fan-out size | `ingest_fanout_top_k` / `ingest_fanout_threshold` | Lower `top_k` or raise the threshold; setting `top_k 1` effectively stops fan-out without disabling ingest |

### 🚫 What a rollback must never do

**`.mb/sources/` and `.mb/sources.json` are content, not state.** No rollback
step above touches them, and none should be added that does — deleting them
destroys the only copy of every ingested document (decision D1, risk R8). The
same holds for `mb uninstall` and `mb doctor --fix`: neither removes or
modifies anything under `.mb/sources/`, and a regression test asserts it by
comparing file hashes before and after all three operations
(`tests/test_release_gate.py::test_sources_survive_uninstall_doctor_fix_and_rollback`).

If you genuinely want them gone, that is a deliberate manual act — and take
`mb ingest export <dir>` first.

### Reverting `schema_version` 2 → 1

Rarely needed (v2 adds only defaulted keys, and an older build ignores unknown
keys with a warning). If you must:

```bash
mb backup list                    # find a pre-upgrade backup id
mb backup restore <id>            # restores config.json; raw sources are only ADDED, never removed
```

or hand-edit `schema_version` back to `1` and run `mb config validate`. The
new keys can stay — an older build treats them as unknown and ignores them.

After rolling back, `mb doctor` and `mb selftest` must both come back green;
if they don't, the report names the exact next command.

## Reverting to the narrower skill set

`mb-onboard` and `mb-triage` are new skill directories — delete
`~/.claude/skills/mb-onboard` and `~/.claude/skills/mb-triage` (they're
junctions/symlinks back to the repo, so this never touches the repo itself)
if you don't want them discoverable. `mb-memory` and `compaction-plan` grew
new capabilities **in place** rather than being duplicated into new skills
(see the T15 decision note in the vault, `[[T15 brainstorm and council
portability decision]]`, for why) — there is no narrower version to revert
to short of checking out an older commit of `singularity/skills/mb-memory/
SKILL.md` / `singularity/skills/compaction-plan/SKILL.md` specifically; the
new behavior (explicit `--project personal` routing, the ≤120-line bound
and git-reconcile step on compaction-plan pickup) is additive guidance, not
a breaking behavior change, so reverting it is rarely necessary.

## Reverting the scheduler job migration

If you specifically want the OLD standalone `nightly-index` job (`index
update` only, no digest/curator/config/trend/markers) back instead of the
new `nightly` job: `mb uninstall` removes both `nightly` and any leftover
legacy `nightly-index` registration, then re-register the old one manually
with your platform's scheduler pointed at `mb index update` — this is
deliberately not a one-command rollback, since the new job is a strict
superset (it always starts with the same `index update`) and there is no
supported reason to prefer the narrower one going forward.

## Full uninstall

```bash
mb uninstall
```

Removes: Claude/Gemini/Codex/Grok hooks and managed profile blocks (only
entries we own — see [UPGRADE.md](UPGRADE.md#foreign-hooks-and-settings)),
the `nightly`/`watch` scheduled jobs, the search daemon process, and skill
junctions that resolve back into this repo. For Grok specifically that is
exactly three artifact kinds under `$GROK_HOME` (else `~/.grok`): the
`SINGULARITY:BEGIN/END` block in `AGENTS.md` (your own text around it is
preserved byte-for-byte), the `hooks/singularity.json` file (only when it
contains our `grok_stop_capture.py` command — a foreign file at that name
is left untouched), and `skills/<name>` links that resolve into this repo.
**Never touches**: your vault, your notes, `~/.singularity.json` or any
`.mb/*.json`/`*.jsonl` snapshot or journal file, foreign hooks, foreign
skills, credentials, Grok's `config.toml`/`sessions/`/`auth.json`, or any
third-party CLI (Codex/Gemini/Grok themselves are never uninstalled).
Re-running the installer afterward picks up exactly where you left off —
nothing about your vault's content or history is lost by uninstalling and
reinstalling the engine.

## Disabling only the Grok capture nudge

You don't need an uninstall for that: `mb hooks disable` turns the desired
`auto_capture` state off for **all** clients at once (Claude, Gemini, Grok
— the hook files stay installed but become no-ops; `mb hooks status` shows
`effective: false`). To remove just the Grok hook file while keeping the
profile and skills, delete `<grok-home>/hooks/singularity.json` — the next
installer/`mb doctor --fix` run re-creates it, so pair the deletion with
`mb hooks disable` if you want it to stay gone in effect.

## If a rollback leaves the system in a confusing state

`mb doctor` (or `mb doctor --fix` for the safe, non-destructive repairs)
always reports the actual current state — registered/not, enabled/not,
matching/drifted — rather than assuming. `mb status --json` gives the same
picture as one machine-readable snapshot.
