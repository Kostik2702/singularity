---
name: mb-ingest
description: Swallow an external document into memory and spread it across the linked wiki pages (the Singularity ingest cycle). Use when the user says "swallow this document", "add a source", "work this article into memory", ingest, "add source", "read it and write it into the vault", or whenever external material has appeared (an article, a technical document, a spec, a thread) whose knowledge should settle in the vault with provenance.
---

# mb-ingest — from a document to an updated wiki

The engine supplies the numbers (storage, provenance, candidates, the log). The retelling, the page edits and the verdicts are your work (decision D5). Exactly like `digest`/`curator`: report-only, applied by a human or an agent.

Raw material lives in `.mb/sources/` — **outside** the Obsidian tree (decision D1), so a source is invisible in the graph and cannot be referenced with a `[[wikilink]]`; provenance is `sources: [<id>]` in the frontmatter.

## Hard gates (a violation rolls the step back)

- [ ] **Edit only on a non-empty `why`.** For every `touch` element the engine must supply `why: {source_section, note_section}`. No `why` — the page is not touched.
- [ ] **"Nothing to add" is a legitimate outcome.** It must be RECORDED (`mb ingest mark` without `--notes`), not passed over in silence.
- [ ] **Add-only.** Append a paragraph or bullet with a quotation from the source. Nothing is rewritten from scratch, nothing is deleted.
- [ ] **The `ingest_fanout_top_k` ceiling (15).** That is a ceiling on SUGGESTIONS, not a plan of mandatory edits.
- [ ] **The network is not in the core (D4).** `mb ingest add` does not download URLs. You download them.
- [ ] **Raw is immutable (D6).** Never edit a file under `.mb/sources/`. A new version means a new `mb ingest add`.

## Steps

### 1. Prepare the content

A local file — the path as it is:
```bash
mb ingest add "D:\docs\article.md" --title "Title" --json
```

A URL — the CLI refuses with exit 2 (not a bug, this is decision D4). You download it yourself and pass it through stdin:
```bash
# 1) get the text (WebFetch / curl / browser -> file)
# 2) hand it to the engine, preserving the origin:
Get-Content article.txt -Raw | mb ingest add --stdin --origin "https://example.com/article" --title "Title" --json
```

- [ ] `--project` only if the material really is project material; a personal source will not enter fan-out at all.
- [ ] The response carries `source_id`, `duplicate`, `stored_path`, `indexed`. `duplicate: true` means the document is already there — that is not an error.

### 2. Catch the index up

The watcher does **not** see `.mb/` (a consequence of D1). If the response says `indexed: "pending"`:
```bash
mb ingest index
```

### 3. Get the plan

```bash
mb ingest plan <source_id> --json
```

Read the fields:
- `summary_target` — where the summary note goes (plus a ready command);
- `touch[]` — up to 15 existing pages with a `score` and a mandatory `why`;
- `new_pages[]` — entities of the source with no page of their own;
- `conflicts[]` — candidate contradictions against what the vault already claims (**candidates, not verdicts**);
- `suppressed` — how many pages are already marked processed for this version of the source.

Refusals to read literally: an `error` about `mb ingest index` means the source is not in the corpus yet; an `error` about a model mismatch means the corpora were built by different models and the plan is deliberately not constructed.

### 4. Read the source and discuss the takeaways with the user

- [ ] `mb ingest show <source_id> --excerpt 4000` for a quick look; the full text comes from `stored_path`.
- [ ] **Do not skip this step.** First 3–7 points to the user in your own words, then the list of pages from `touch` you propose to touch, each with one line of "what exactly I will add".
- [ ] Wait for confirmation or a correction of the list.

### 5. The summary note

```bash
mb note new reference "<Title>" --from-source <source_id>
```
The command sets `sources: [<id>]` in the frontmatter itself and the back-reference in the registry. Then fill the note in: context, substance, what follows from it for us, links to the pages touched.

### 6. Edits to existing pages

For every `touch` element the user approved:
- [ ] open the page and find the section from `why.note_section`;
- [ ] add a paragraph with a **quotation** from `why.source_section` and a note of the source;
- [ ] if there is nothing to add — skip it and say so; that is a normal result, not a failure.

### 7. Record the outcome

```bash
mb ingest mark <source_id> --notes "reference/a.md,reference/b.md" --status processed --actor skill
mb ingest mark <source_id> --actor skill              # the "nothing to add" outcome
```
Marked pages stop surfacing in the plan (for this version of the source). Re-ingesting a changed document brings them back — it gets a new `source_id`.

### 8. The human trail

- [ ] A line in `changelog/YYYY-MM.md`: what was swallowed, which pages were touched, what stayed open.

### 9. Report to the user

- [ ] `source_id`, the number of pages touched, what stayed open, `conflicts` (if any) explicitly marked "candidates, awaiting a verdict".
- [ ] Check that the loop closed: `mb curator gaps --json` — this source must not remain in `unprocessed_sources`.

## Interaction with auto-capture

Page edits from this cycle are not session insights. Do not run `mb learn` during an ingest: duplicates in `learnings/active` are a known risk (R5). The knowledge is recorded in the pages themselves and in the `changelog`.

## Diagnostics

```bash
mb ingest list --status new          # what is not digested yet
mb ingest log --limit 20             # the cycle's history (machine-readable)
mb ingest export "D:\backup\sources" # emergency dump of raw material out of .mb/
mb search "<phrase>" --with-sources  # escalation: the wiki has no answer, look in the raw material
```
