---
name: mb-memory
description: Capture knowledge into the user's Obsidian memory vault (Singularity). Use when the user says um, "remember this in the vault", "write it to memory", "add to memory brain", after completing a phase of work on the Singularity project, or when an important decision/insight/mistake emerges that should persist. Defines what deserves recording and the note contract.
---

# mb-memory — writing knowledge into the vault

The vault resolves itself (`--vault` → `MB_HOME` → `~/.singularity.json` → defaults); where it is on this machine is what `mb doctor` reports (check `A2-vault-obsidian`). The path is not hardcoded here: until 2026-08-04 this line held one machine's absolute path, which on macOS/Linux pointed at nothing.

The watcher indexes what you save automatically (~5-15 s) — nothing needs to be run after a write.

## What to record (the token criterion)

- [ ] Record ONLY: the non-obvious / what contradicts expectations / what took more than one attempt / a decision with consequences.
- [ ] Do NOT record: anything visible from git or the code, trivial facts, retellings of files. Code goes in as `file:line` only.
- [ ] Before writing — `mb search "<the gist>"`: if something comes back with score >0.6, extend the existing note instead of creating a duplicate.

## How to record

```bash
mb learn "<a dense insight in one to three sentences>" --task <TASK-ID>   # quick append to learnings/active/<TASK-ID>.md
mb note new decision "Name of the decision"    # a new note from the template
# types: learning|decision|howto|incident|rule|reference|daily|moc|changelog — one per managed root
mb note new rule "X" --filename "Rule - X"   # vault convention: type in the filename, title kept clean
```

- [ ] Personal material (belonging to no work project) — add `--project personal` to `mb learn`/`mb note new`, or, if the entry is already being created in a directory from `personal_roots` (see the machine config `~/.singularity.json`), routing applies automatically. Personal notes never reach digest/curator suggestions and are never resolved by an accidental cwd detection — only by an explicit `--project personal` or by personal_roots.

### GATE before any write: where to write

- [ ] The destination is resolved BEFORE the first write. If the artefact is meant
      for the vault — write **straight into the vault** (or with an `mb` command),
      not into a copy.
- [ ] **No editable mirrors**: a staging copy of a vault document in an external
      folder with a later copy-back is forbidden — including for the sake of an
      editor, a sandbox or a patch tool. A second working copy stays a second
      source of truth until somebody compares their hashes.
- [ ] External directories are INPUT (reading sources) and explicitly requested
      exports. Neither grants the right to an editable duplicate.
- [ ] If writing directly into the vault really is blocked (permissions, sandbox) —
      STOP and say so, asking for access or for a vault-native path. Routing
      around the block through staging is not allowed.

The incident this rule came from: [[Error - Vault staging duplicates]] — 14
working copies of notes in the home directory plus copy-back, because the sandbox
showed that folder as writable. No unique data was lost, but for a while the
machine held two working sets of the same notes, and only a hash audit could say
which was canonical. The rule "artefacts live in the vault" already existed — it
was absent at the point where the write path is chosen.

### GATE before any write: what to write with

- [ ] **A new file in a managed root** (`learnings/*`, `decisions/`, `howto/`, `incidents/`, `rules/`, `reference/`, `changelog/`, `_moc/`, `daily/`) — ONLY `mb note new <type> "<title>"`. The size of the entry is irrelevant: create the file with the command, then fill it in with Edit.
- [ ] **Editing an existing note** — Edit/Write, that is the normal path.
- [ ] **Config** — `mb config set` / `mb project add`, never a hand-edit of `~/.singularity.json` (the machine config; it has not lived in the vault since v0.9.0).
- [ ] **Searching the vault** — `mb search`, `mb curator gaps` (broken links, undigested learnings), `mb curator contradictions`, rather than `grep` or walking files.

Why this is a gate and not a recommendation: bypassing `mb note new` skips `templates/<type>.md` — and the write does NOT fail, the note is simply born without `source:` and without the mandatory sections the template carries (for a rule: the statement, the failure it catches with its GATE, and the origin). Such a note is indistinguishable from a complete one until somebody compares it against the template by hand; that is exactly what happened on 2026-08-04. Project routing and dedup are lost as well.

`vault_guard` (audit mode, on by default since v0.6.3) answers such a Write with the line `[vault-guard audit] New file created directly in managed knowledge root …` — that is a signal to stop and recreate it with the command, not background noise.

While filling the note in, keep to the contract:

- [ ] Frontmatter is mandatory: `type, title, tags, created`; for a decision also `owner` and `source` (Never Decide Alone: a decision has an author and an origin).
- [ ] Links are inline `[[wikilinks]]` to existing notes (they feed the Obsidian graph and the search graph filter).
- [ ] A change to something significant gets a line in `changelog/YYYY-MM.md` (the single history; notes themselves are not inflated with edit history).
- [ ] Never delete what is outdated: `status: superseded` plus a link to the replacement.

## Mistakes → rules (self-teaching)

- [ ] If an assistant mistake repeated or cost time: create `mb note new rule "..."` — the vault note holds the WHY (the incident, the cost, the origin). GATE: "would this rule have caught precisely this failure?" — if not, the rule is not needed.
- [ ] **And graft it into the point where the corresponding decision is made — in the engine, not in the vault.** A note in `rules/` is an archive of knowledge; what acts is the text standing at the point. The real points and their audiences:
      `singularity/machine/hooks_state.py` (a SessionStart fragment → the main thread) ·
      `singularity/agents/AGENT_SHARED_PREAMBLE.md` §2 (every subagent) ·
      `singularity/agents/<agent>.md` (one agent) ·
      `singularity/skills/<skill>/SKILL.md` (that class of work) ·
      a docstring or a default in the code (writing that kind of code).
      A rule that must act without a vault cannot live in the vault: swapping or losing the memory changes the available knowledge, never the machine's behaviour.
- [ ] Several points — ONE source injected into all of them (hand-written copies drift), plus a **presence test** in `tests/test_rules_delivery.py` that fails when the injection is removed from any surface. The test is checked the way the rule itself demands: delete the injection, watch the suite go red, put it back.
- [ ] Check the counter-text: if the opposite is already written at that point, the instruction wins, not the rule. Reconcile them in the same pass.
- [ ] Update the rule's entry in `docs/rules-delivery.md` (the "rule → point" registry) and add a "Where it is grafted" section to the note itself.

## End of a project session

- [ ] One question to the user: "Finalise the learnings?" — and on the answer, append the session's outcome to the active learnings document.
