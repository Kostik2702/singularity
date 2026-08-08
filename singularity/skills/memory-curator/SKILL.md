---
name: memory-curator
description: Vault health check, contradiction adjudication, research agenda AND adoption review for the Singularity memory (Obsidian). Use when the user says curator, "check vault health", mdoc, "suggest links", "what should I research next", "are there contradictions"; also for "are we actually using the memory", "is the memory working for me", adoption, reflect, "memory stats", "is this worth maintaining"; or periodically after large writing sessions. Runs mb curator / contradictions / gaps plus mb reflect / stats, reviews findings, proposes fixes - NEVER applies structure changes without user approval.
---

# memory-curator — vault health, contradictions and the agenda

Three tools, all report-only:

| Command | What it gives |
|---|---|
| `mb curator [--json]` | mechanical validation + semantic link suggestions |
| `mb curator contradictions [--json] [--limit N] [--same-file]` | CANDIDATE contradictions with evidence tokens |
| `mb curator gaps [--json] [--top N] [--heuristic]` | what the memory is missing |

## The curation session protocol

- [ ] 1. Run `mb curator --json` and work through the output (issues / orphans / stale / link_suggestions).
- [ ] 2. **Broken wikilinks** — fix them yourself (this is mechanics, not structure): find the right target with `mb search`, correct the link. If the target does not exist — ask the user (delete the link or create the note?).
- [ ] 3. **Missing frontmatter** — fill it in from the note's context (title from H1, created from file ctime, type from the folder). User files outside the contract (vault root, daily) — do NOT touch, only report.
- [ ] 4. **Decisions without owner/source** — do NOT invent an owner (Never Decide Alone): ask the user or mark `owner: UNRESOLVED`.
- [ ] 5. **Link suggestions** — show the user a list of "A ↔ B (score)" with a one-line justification per pair; apply `[[wikilink]]` ONLY for approved ones (insert into the "Related" section, not into a random spot in the text).
- [ ] 6. **Orphans** — for each, propose: link it (see suggestions), fold it into a MOC, or accept it as a legitimate standalone (daily/changelog).
- [ ] 7. **Stale active** (>60 days) — propose `status: superseded`/archive; never change without approval.
- [ ] 8. Wrap up: a compact report (fixed myself / proposed / rejected) plus an entry in the vault changelog if anything changed.

## Contradiction adjudication (W07)

`mb curator contradictions` yields pairs of "one topic — different claims" in the cosine band 0.72–0.90 (above that it is a near-duplicate, which `mb curator` handles; below, they are different topics). **These are candidates, not verdicts**: precision without an LLM is low by construction, which is exactly why every candidate must carry `evidence` — the tokens that raised it.

- [ ] 1. `mb curator contradictions --json`. Read `evidence`, not just the score: `boolean_flip`/`polarity_flip`/`negation_flip` are strong signals, `numeric_mismatch` is medium, `negation_asymmetry` is a modifier.
- [ ] 2. For each candidate open BOTH sections in full. Typical false positives: differing scope ("enabled for A, disabled for B" is not a contradiction), a comparison table, a versioned recipe (v3 next to v4), a quotation of somebody else's decision.
- [ ] 3. A verdict only ever comes with a one-line justification. Record it like this:
```bash
mb curator verdict "<key>" --verdict false_positive --note "different scope: A is about a personal vault, B about a shared one"
mb curator verdict "<key>" --verdict resolved      --note "fixed reference/x.md, now 0.45 in both"
mb curator verdict "<key>" --verdict accepted      --note "a real contradiction, the owner decides"
```
A marked pair stops resurfacing — without this the same false positive comes back every run and people stop reading the report.

- [ ] 4. **Not a single note edit without approval.** If a `resolved` verdict requires a change — first show the user both sides and propose which one to edit.
- [ ] 5. Turn on `--same-file` only when auditing one long note: inside a single document a numeric divergence is almost always a comparison table.

Optional: batch adjudication by a local model (`mb llm` + the `mb-llm-delegate` skill) behind the `lint_llm_adjudication` flag (`false` by default). The model's verdict is still a proposal, not an application.

## The research agenda (W08)

`mb curator gaps` answers "what should I research next", not "is it tidy". Six signals: `unprocessed_sources`, `open_questions`, `missing_pages`, `undigested_learnings`, `decisions_without_review`, `unlinked_from_index`.

- [ ] 1. `mb curator gaps --json`. `counts` are the true totals; `items` is the shortlist after the per-signal ceiling; `truncated` tells you what is hidden.
- [ ] 2. `unprocessed_sources` → hand it to the `mb-ingest` skill (the loop is not closed).
- [ ] 3. `open_questions` → offer the user 1–2 to close right now; leave the rest.
- [ ] 4. `missing_pages` → these are pages referenced by ≥2 notes that do not exist. Create them only on approval.
- [ ] 5. `undigested_learnings` → this is input for `mb digest plan`, not for a manual edit.
- [ ] 6. `--heuristic` — guesses by frequency; enable it deliberately and label them as guesses in the report.

## Health from the data (T14)

Three numbers answer the question `mb curator` never asks: **is the memory working for the user at all**. The curator repairs the vault; this section decides whether repairing it further is worth it. T14's goal, verbatim: "measure adoption and decide about further complexity from data, not from impressions".

| Command | What it gives | What to look at |
|---|---|---|
| `mb reflect [--days N] [--json]` | share of sessions carrying each adoption signal | `search_then_read`, `vault_touch` against `eligible_research` |
| `mb stats [--json]` | the composition of the memory | `sources: new=N`, age of the code index, skew by type |
| `mb nightly --json` | the nightly run + a trend line | the direction, not the absolute |

**There are no thresholds in the engine, and that is not an oversight**: the only declared goal they could be tied to is G1 from `02_New_System_Design/00_Vision_And_Principles.md` ("≥80% of a session's knowledge reaches the vault without an explicit command"). Everything else is judgement about a trend, and the report has to call it judgement.

- [ ] 1. `mb reflect --days 30`. The DENOMINATOR first — the number of sessions. On a sample below ~10 the percentages mean nothing: say so out loud and draw no conclusions (today on this machine there are 3).
- [ ] 2. `eligible_research` — sessions where knowledge was present but no code was written (the metric exists precisely so they do not drop out of the denominator). A high share of them with a low `vault_touch` means knowledge is not settling — a direct divergence from G1. The action is not "write more notes" but find the break: hooks disabled (`mb hooks status`), skills not invoked (`skill_invocation`).
- [ ] 3. A low `search_then_read` means SEM-FIRST is not being followed — memory is searched less often than files are read. The action is to check whether the SessionStart prompt is arriving (it is the carrier of the rule), not to rewrite skills.
- [ ] 4. `mb stats`: `sources new=N > 0` means the ingest loop is not closed. This is the same signal as `unprocessed_sources` in `mb curator gaps`, and it goes to the same place — the `mb-ingest` skill, not a manual edit.
- [ ] 5. The report is exactly three lines: what was measured → what follows from it → which ONE action is proposed. A metric with no proposed action is noise that people stop reading (the same principle as verdicts in adjudication).
- [ ] 6. If the numbers say "the system is used less than it costs to maintain" — say it plainly. T14 was built for that conclusion too, otherwise the measurement only serves itself.

## Write discipline

- [ ] Zero structural writes without explicit approval. Autonomously allowed: fixing broken links with an unambiguous target, filling in mechanical frontmatter.
- [ ] **Archiving is where knowledge is lost, not a change of status.** `learnings/archive/` is NOT part of the semantic index: whatever remains only there disappears from `mb search`. Before setting `status: archived` and moving the file: read the learning in full → lift out the insights that exist in no permanent note (a fact about a tool → `reference/`, an order of operations → `howto/`, a choice with consequences → `decisions/`, a recurring mistake → `rules/`) → verify by search that what you lifted can be found (`mb search "<verbatim phrase>"` returns the new note) → and only then archive. The tell-tale of a violation: between reading the learning and moving it, not one edit appeared in any permanent note.
- [ ] Verdicts in `.mb/lint.json` are engine state, not vault structure: they may be written without approval, but each with a justification.
- [ ] Dashboards for manual review: `_moc/Dashboard.md`, `_moc/Orphans.md` (Dataview).
