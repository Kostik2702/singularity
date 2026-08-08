---
name: design-reference-scout
description: Collect design references from open sources (Openverse, Are.na, Httpster, Minimal.gallery, Land-book, optionally Unsplash/Pexels) into an HTML moodboard with provenance and licences for the owner's acceptance. A thin wrapper over the deterministic `design-scout` CLI. Use when "collect references", "moodboard for the design", "design references", "inspiration for the site", art-direction search for examples, at the start of the visual design of a new site or section.
---

# design-reference-scout — a moodboard from open sources

All the deterministic work lives in the `design-scout` CLI (the Singularity engine, contract `singularity/scout/CONTRACT.md`, **schema_version 1**). The skill holds no logic: it says WHEN to apply it, HOW to build the queries, HOW to read the output and WHERE the human gate is.

> **Zero-code rule (WSF-13 trajectory invariant):** tempted to write a script to "visit a site / download a preview / assemble HTML" — STOP, those are already `design-scout` subcommands. A new source is a line in `sources.json`, not code. A scout session creates **0 new scripts**.

## When to apply it

- Starting the design of a new site or section: a moodboard is needed for the art direction.
- Topping up references for a specific aesthetic ("dark terminal", "brutalist", "minimal").
- Not for: collecting installable components or code (that is WSF-E02 pattern-source-scout), and not for production-quality assets (that is licensed purchase or a shoot).

## The order

```bash
# 1. Source health (evaluative: floor of "≥3 alive, ≥1 API" from the registry)
design-scout probe --json

# 2. Collection: 3-6 short queries
design-scout collect \
  --query "matrix terminal aesthetic" --query "code rain" \
  --query "dark minimal web design" \
  --out "E:\Project\<project>-evidence\scout-<date>"
# --out lives OUTSIDE the project tree; DSCOUT_PROTECTED_ROOTS=<project> physically forbids writing into the project

# 3. Open moodboard.html, review it, take it to the owner for acceptance
```

## How to build queries (the LLM part — the only legitimate one)

- Unfold the art-direction brief into **short (1-3 word), concrete** queries: an aesthetic ("dark terminal"), a device ("code rain"), a genre ("brutalist portfolio").
- The sources are English-language, so English queries carry the search; a query in another language is a supplement for Openverse.
- Long phrases ("a beautiful dark site with animation") return noise or nothing — split them into atoms.
- Httpster does not search — it routes by style keywords (dark, minimal, brutalist, typographic, retro, interactive…): include at least one such word in the query set.

## How to read the output (contract §2, §6)

- Pin `schema_version == 1`; an unknown version is a loud stop.
- `collect` exits 0 on a **completed run**, even if some sources came back `unavailable:*` — look at `data.sources`: typed statuses (`live`, `unavailable:http-403`, `skipped:no-key`, `skipped:disabled`, `skipped:budget`).
- exit 2 means the tool did not run (a corrupt registry, a forbidden --out). That is NOT "no results".
- `data.budget_log` is the evidence that budgets were respected (fetches/retries/thumbs/elapsed/stop_condition) — record it in the run's learnings.

## Licences (OD-2, owner 2026-07-14 — not to be reinterpreted)

| policy badge in the moodboard | What is allowed |
|---|---|
| `api-free-license` (Openverse, Pexels) | the preview is downloaded locally, each entry carries its licence — use under its terms, attribution mandatory |
| `api-hotlink` (Unsplash) | hotlink-embed of their URL plus attribution only; downloading is forbidden by the ToS |
| `links-only` (galleries) | a link or embed for inspiration; **downloading and copying the design are forbidden** — browsing ≠ a right to store |
| `per-block` (Are.na) | per the block's licence; unknown means link only |

`license: unknown` NEVER means "usable".

## Owner gates (Never Decide Alone)

1. **Moodboard acceptance** — the input to the design is approved by the owner; a moodboard is not approval by itself.
2. **Godly / SiteInspire** are disabled: their robots.txt forbids AI crawlers. Enabling them takes an explicit owner decision (editing `enabled` in sources.json with a record in the vault).
3. **Unsplash/Pexels** are enabled with the keys `UNSPLASH_ACCESS_KEY` / `PEXELS_API_KEY` (the owner creates the accounts; demo limits are 50/200 req/h).
4. Paid sources are out of scope, report-only (the v2 expansion policy).

## Recording the result

The run (queries, source statuses, budget_log, the moodboard path) goes into the task's learnings via `mb learn`. A moodboard the owner accepted is linked into the design brief (the input to WSF-06 design-analysis).
