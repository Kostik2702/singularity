---
name: mb-triage
description: Inverse-Trust bug/incident triage -- search memory before touching code, and capture only when the root cause is a recurring class. Use when the user reports a bug, error, or unexpected behavior, or says triage this, "sort out this error", "why is this not working".
---

# mb-triage — Inverse Trust for bugs and incidents

Two "Inverse Trust" principles: (1) do not trust your first impression of the cause — the vault may already have seen this class of problem, so check BEFORE you start reading code; (2) do not trust the impulse "this needs recording" — most bugs are one-offs, and a record is justified only for a RECURRING class of cause.

## Protocol

- [ ] 1. **SEM-FIRST, before the code**: `mb search "<the symptom or error message in your own words>"`. Phrase it however you would naturally say it — retrieval is crosslingual, so a note does not have to be written in the language you ask in. This is a SEPARATE step from the usual SEM-FIRST for knowledge — here the target is specifically `incidents`/`rules`, not notes in general.
- [ ] 2. Is there a relevant match (`--type incident` or `--type rule`, score>0.5)? Read the note in full, apply the recorded solution, and cite the source to the user. Do not start the investigation from scratch — that saving is exactly what the memory is for.
- [ ] 3. No match — investigate the normal way (use `superpowers:systematic-debugging` if it is available, or the project's standard debugging process). mb-triage does not replace the debugging process; it only gates it with a SEM-FIRST check at the start.
- [ ] 4. **After the fix** — before deciding whether to record it, answer this question explicitly: "will this cause recur in another file, module or project, or is it a one-off accident (a typo, a forgotten import, a one-time environment race)?". Record ONLY the recurring class.
- [ ] 5. Recurring — `mb note new incident "<a short name for the class of cause>"` (not "the title of this particular bug", but the name of the PATTERN, so a future `mb search` on a similar symptom finds it). One-off — do not create a note; if you like, `mb learn` one line into the current learnings document purely for the session journal, with no incident created.

## Do not

- [ ] Do not skip step 1 "because the cause is obvious anyway" — those are exactly the cases that most often turn out to be an already documented incident with a solution that was not obvious at first glance.
- [ ] Do not create an incident note for every bug you catch — that is token inflation of the vault. The GATE (as in `mb-memory`): "would this note have caught precisely THIS repeat occurrence, had it already been written?" — if not, do not write it.
- [ ] Do not confuse the triage search (step 1, `--type incident,rule`) with the ordinary SEM-FIRST over knowledge in general — they look for different things across different note types.
