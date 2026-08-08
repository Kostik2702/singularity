# The ingest cycle — from a document to an updated wiki

Singularity's implementation of the LLM-wiki method: an external document is
stored immutably with provenance, and the knowledge in it is deliberately
spread across the ten-to-fifteen existing pages it actually touches, instead of
becoming one more note nobody links to.

**Design:** [`wiki-loop-design.md`](wiki-loop-design.md) ·
**Decisions:** vault `decisions/WIKI-LOOP D1…D7` ·
**Baseline:** [`wiki-loop-baseline.md`](wiki-loop-baseline.md)

---

## 1. What it is for

Capturing a document is easy and nearly worthless: a summary note in isolation
does not make the vault smarter. The value is in the *fan-out* — the source
says something about retention policy, so the retention rule, the nightly
howto, and two design docs each gain a line with a citation. That is what makes
knowledge compound instead of accumulating.

Three properties make the fan-out trustworthy rather than a licence to pad
notes with filler:

* every proposed page comes with `why` — which passage of the source matches
  which section of the page. No evidence, no edit;
* "nothing to add here" is a legitimate, recorded outcome;
* the raw document is kept byte-for-byte, so any claim derived from it stays
  checkable.

## 2. Where things live

| Path | What | Recoverable? |
|---|---|---|
| `.mb/sources/<YYYY>/<slug>--<id8>.md` | raw document, write-once | **NO — the only copy** |
| `.mb/sources.json` | registry: id, hash, origin, status, derived notes | **NO** |
| `.mb/sources_index/` | second search corpus over the raw layer | yes, rebuildable |
| `.mb/ingest-log.jsonl` | machine history of the cycle | no (but non-critical) |
| `.mb/lint.json` | contradiction verdicts | no (but non-critical) |

Raw lives under `.mb/` and **not** in the vault note tree (decision D1), so
Obsidian's graph, `mb search`, `mb curator` and `mb digest` never see it — by
construction, not by filter. The price: sources are invisible in Obsidian and
cannot be `[[wikilinked]]`; provenance is `sources: [<id>]` frontmatter.

> ⚠️ `.mb/` is documented as runtime state, which makes "delete `.mb` and
> re-index" a natural reflex. It is now destructive. `mb backup create` covers
> the raw layer with checksums, `mb doctor` FAILs when the registry outlives
> its files, and `mb ingest export <dir>` copies everything out.

## 3. The cycle

```
title Ingest cycle (one source)

participant User as U
participant "Agent / skill mb-ingest" as A
participant "mb CLI (engine)" as CLI
participant ".mb/sources/ (raw, outside Obsidian)" as S
participant ".mb/sources_index/ + memory_index" as IDX
participant "wiki notes" as W

U->A: "swallow this document"
A->CLI: mb ingest add <path|--stdin> --title ... --origin ...
CLI->S: write-once .mb/sources/<YYYY>/<slug>--<id8>.md (frontmatter + raw verbatim)
CLI->CLI: .mb/sources.json (atomic tmp+replace)
CLI->IDX: incremental update of sources_index (through the warm daemon)
CLI-->A: {source_id, duplicate, stored_path, indexed: indexed|pending}
A->CLI: mb ingest index (if indexed=pending — catch up explicitly)
A->CLI: mb ingest plan <source_id> --json
CLI->IDX: read both committed generations (no model, no writes)
CLI-->A: {summary_target, touch[<=15] with why, new_pages, conflicts}
A->U: takeaways + the list of pages to edit
U-->A: confirmation / correction of the list
A->W: summary note (mb note new ... --from-source <id>)
A->W: edits to N existing notes (add-only, with a quotation)
A->CLI: mb ingest mark <id> --notes a.md,b.md --status processed
CLI->CLI: derived_notes + marks + a line in .mb/ingest-log.jsonl
A->W: a line in changelog/YYYY-MM.md
A-->U: report: what was recorded, what stayed open
```

Rendered at [sequencediagram.org](https://sequencediagram.org/instructions.html).

The semantic half of this — the retelling, the edits, the verdict on a
conflict — is the `mb-ingest` skill's job, never the engine's (decision D5).
The engine only stores, counts and journals; `ingest_plan.py`,
`contradictions.py` and `gaps.py` contain no write path at all, enforced by
`tests/test_wiki_loop_report_only.py`.

## 4. Commands

| Command | Does | Notes |
|---|---|---|
| `mb ingest add <path>` / `--stdin` | store one document write-once | refuses `http(s)` URLs (D4) — fetch it yourself and pipe with `--origin` |
| `mb ingest list [--status] [--limit]` | registry listing, newest first | `--status new` = not yet turned into notes |
| `mb ingest show <id> [--excerpt N]` | metadata + a bounded excerpt | never dumps the whole document by default |
| `mb ingest index [--rebuild]` | build/catch up the sources corpus | needed because the watcher does not see `.mb/` |
| `mb ingest plan <id> [--top-k N] [--threshold F] [--summary-type T]` | fan-out candidates | report-only, offline, deterministic |
| `mb ingest mark <id> [--notes a,b] [--status] [--actor]` | record what the source produced | no `--notes` = the recorded "nothing to add" outcome |
| `mb ingest log [--since] [--op] [--limit]` | read the machine history | one JSON object per line |
| `mb ingest export <dir> [--id] [--force]` | copy raw + registry out of `.mb/` | copies, never moves |
| `mb ingest backfill [--apply]` | retro-provenance from existing notes | dry run by default |
| `mb search "..." --with-sources` | escalate into the raw layer | sources never appear without the flag |
| `mb curator contradictions [--limit N] [--same-file]` | contradiction CANDIDATES with evidence | precision without an LLM is low by design |
| `mb curator verdict <key> --verdict V [--note]` | silence a judged pair for good | `false_positive` / `resolved` / `accepted` |
| `mb curator gaps [--top N] [--per-signal N] [--heuristic]` | research agenda | `counts` are true totals, `items` is the shortlist |
| `mb backup create [--with-sources\|--no-sources]` | snapshot incl. the raw layer | skips large collections with a warning |

## 5. Configuration

| Key | Default | Meaning |
|---|---|---|
| `ingest_index_sources` | `true` | kill-switch for the second corpus |
| `ingest_max_source_chars` | `400000` | oversize source is refused, never truncated |
| `ingest_fanout_top_k` | `15` | ceiling on plan suggestions (D7) |
| `ingest_fanout_threshold` | `0.45` | min cosine for a candidate page |
| `ingest_log_retention_days` | `365` | `.mb/ingest-log.jsonl` retention, swept by nightly |
| `immutable_roots` | `[".mb/sources"]` | guard denies Write/Edit here even in `audit` mode |
| `gap_link_min_inbound` | `2` | inbound links before a dangling link is a missing page |
| `gap_learning_age_days` | `30` | age at which an un-digested learning is a gap |
| `lint_llm_adjudication` | `false` | optional batch adjudication via `mb llm` |
| `backup_max_sources_mb` | `200` | warn-and-skip threshold for backing up raw |
| `encoder_device` | `null` | `null` probes CUDA with a real kernel launch, falls back to CPU |

## 6. Limits — what this deliberately does not do

* **No network in the engine.** `mb ingest add` reads a file or stdin. URLs are
  refused with the command that works. The offline invariant is worth more than
  one saved step.
* **Contradictions are candidates.** Same subject + opposite claim is a
  hypothesis; "enabled for A, disabled for B" looks identical to a model and
  is not a conflict. Every candidate carries evidence tokens precisely so a
  human can dismiss it in seconds — and `mb curator verdict` makes that
  dismissal permanent.
* **Same-file contradictions are off by default.** Inside one note a differing
  number is almost always a comparison table or a versioned recipe. `--same-file`
  turns them on when auditing a single long document.
* **`new_pages` is not entity extraction.** Only unresolved wikilinks and
  entity-shaped section headings. Frequency-based guessing lives behind
  `mb curator gaps --heuristic` and is labelled as a guess.
* **Personal sources do not fan out.** Personal material is never promoted into
  shared notes, the same invariant `digest` already holds.
* **The guard is the second line, not the first.** `vault_guard_enabled`
  defaults to `false`, so immutability is really enforced by the write-once
  check in `sources.py`; the PreToolUse deny is defence in depth.

## 7. Health

```bash
mb doctor            # block W: registry/disk integrity, corpus freshness, git portability
mb status            # sources: N documents (M unprocessed), K chunks
mb stats             # per-status counts + derived-note totals
mb curator gaps      # what is still open
mb ingest log        # what actually happened
```

`mb nightly` runs the cycle's upkeep: catch the sources corpus up, snapshot
gaps and contradictions, sweep the ingest log, and add the trend fields
(`sources`, `unprocessed_sources`, `open_questions`,
`contradiction_candidates`) to `.mb/nightly-history.jsonl`. Each step is
independently guarded — one failure never sinks the chain.
