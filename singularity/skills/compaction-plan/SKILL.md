---
name: compaction-plan
description: Save full session state to the vault before context clear/compaction so nothing is lost. Use when the user says cp, compaction plan, "save the session state", "before clear", or when context is nearly exhausted (~10% remaining) - proactively offer it.
---

# compaction-plan — saving state before a clear

The full continuation plan is written into the vault, and a new session picks it up through the SessionStart hook (recall) and carries on without losses.

## Protocol

- [ ] 1. Collect the state of the CURRENT session: the active task and workflow phase; what is done (with file:line/paths); what is left (concrete next steps, not "carry on"); open decisions and questions with their owner; important paths, commands and values discovered during the session; uncommitted changes (`git status --short`, saved as a snapshot — it is needed at resume time for the git reconcile).
- [ ] 2. Write it to `{{VAULT}}/compaction-plans/<YYYY-MM-DD-HHmm>-<task>.md` (frontmatter: type=reference, title, created, task, tags:[compaction]). Densely, with no retelling of the dialogue — only what is needed to continue. **Budget: ≤120 lines of body** (frontmatter excluded) — this is a single-use operational artefact for resuming, not a session report; if it does not fit, cut implementation detail (it is in the git diff anyway) and keep decisions, next steps and open questions.
- [ ] 3. Tell the user in one line: the plan is saved, and after the clear a new session will see it automatically.
- [ ] 4. **If the task stops with steps still open — hand the user a ready continuation prompt directly in the chat**, not only the plan in the vault. These are different artefacts for different audiences: the plan is a state archive for memory and search, the prompt is what a person pastes into a new session without editing it. The prompt is self-sufficient: the task's keyword, what is done (as links to notes, not as a retelling), the next step, `[[...]]` to the plan or spec, and open decisions awaiting an answer. The rule applies at any pause (`um`, "let us stop here", an answer to AskUserQuestion), not only at compaction.

## On the resume side (a new session)

- [ ] 1. The SessionStart hook shows pending compaction plans (file names). If the user says to continue — go to step 2; if they picked other work, do NOT touch the plan, leave it until an explicit decision (do not delete it, do not read it proactively).
- [ ] 2. Read the FULL plan plus the active learnings document of the task it references (`task` from the frontmatter).
- [ ] 3. **Reconcile git**: compare `git status --short`/`git log -1` NOW against the snapshot from the plan (recording step 1) — the code may have changed between sessions (a manual commit, a revert, somebody else's edit). If they diverge, check the plan against the real state; do not continue blindly on a stale "what is done"/"what is left".
- [ ] 4. **Durable insight goes separately**: if reading the plan or reconciling surfaced something worth remembering beyond this task — `mb learn`/`mb note new` THAT as a separate call, before deleting the plan. The plan itself is a single-use operational artefact; it does not replace permanent memory (see `mb-memory`).
- [ ] 5. **Delete the plan file** (single-use; otherwise it hangs around forever) → continue from the first open step (as corrected by the reconcile in step 3).
