# Rollback guide

How to turn any specific mechanism shipped in the `SINGULARITY_INCOMING_FORK`
epic back off, without a full uninstall and without losing vault data. Every
rollback below is a config flip, a scheduler/hook removal, or `mb uninstall`
— nothing here deletes a note or a journal file.

## Individual feature flags

All of these live in `<vault>/.mb/config.json`, set with
`mb config set <key> <value>` (validated, atomic write) or by hand-editing
and running `mb config validate` afterward.

| Feature | Flag | Rollback |
|---|---|---|
| Capture nudge (Stop hook) | `auto_capture` | `mb hooks disable` (equivalent to `mb config set auto_capture false`). SessionStart is **not** affected — it has no dependency on this flag at all (structurally verified: `auto_capture` does not appear anywhere in `singularity/hooks/session_start.py`'s source, `tests/test_release_gate.py::test_sessionstart_never_reads_auto_capture_key_at_all`). |
| Module-context injection (PreToolUse) | `module_context_enabled` | `mb config set module_context_enabled false` — default is already `false`; only relevant if you turned it on |
| Vault guard (PreToolUse) | `vault_guard_enabled` / `vault_guard_mode` | `mb config set vault_guard_enabled false` — default already `false`. To keep it on but stop it from blocking, set `vault_guard_mode audit` (warn-only) instead of `deny` |
| Digest nag on SessionStart | `nightly_nag_rate_limit_hours` | Not a hard off-switch by design (it's a rate limit, not a toggle) — set a very large value (e.g. `87600` = 10 years) to effectively silence it, or stop running `mb nightly` (see below) so `.mb/digest-last.json` never refreshes |
| Nightly orchestration | — | `mb doctor --fix` only registers the job if it's missing/drifted; to stop it running at all, remove the scheduled task directly (Windows: `schtasks /Delete /TN Singularity-nightly`; macOS: unload `~/Library/LaunchAgents/com.singularity.nightly.plist`; Linux: `systemctl --user disable --now singularity-nightly.timer`) — `mb nightly` still works fine run manually |

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

Removes: Claude/Gemini/Codex hooks and managed profile blocks (only
entries we own — see [UPGRADE.md](UPGRADE.md#foreign-hooks-and-settings)),
the `nightly`/`watch` scheduled jobs, the search daemon process, and skill
junctions that resolve back into this repo. **Never touches**: your vault,
your notes, `.mb/config.json` or any `.mb/*.json`/`*.jsonl` snapshot or
journal file, foreign hooks, foreign skills, credentials, or any
third-party CLI (Codex/Gemini themselves are never uninstalled). Re-running
the installer afterward picks up exactly where you left off — nothing
about your vault's content or history is lost by uninstalling and
reinstalling the engine.

## If a rollback leaves the system in a confusing state

`mb doctor` (or `mb doctor --fix` for the safe, non-destructive repairs)
always reports the actual current state — registered/not, enabled/not,
matching/drifted — rather than assuming. `mb status --json` gives the same
picture as one machine-readable snapshot.
