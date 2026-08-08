# WIKI-LOOP — design of the sources layer, the ingest cycle and the intelligence lint

**Epic:** `SINGULARITY-WIKI`, tasks `W00…W12`
**Spec:** vault `03_Tasks/SINGULARITY_WIKI_LOOP_IMPLEMENTATION.md`
**Decisions:** vault `decisions/WIKI-LOOP D1…D7` (owner: user, 2026-07-28)
**Baseline:** [`wiki-loop-baseline.md`](wiki-loop-baseline.md)

This document records the contracts and the exact map of integration with the
existing code. Every claim about the current state carries a `file:line` against
the code at commit `ac1d807` (v0.4.0).

---

## 1. What is being built

The LLM-wiki method, in Singularity's terms:

| Element of the method | Implementation | Task |
|---|---|---|
| Raw sources — immutable external documents | `.mb/sources/<YYYY>/<slug>--<id8>.md` + the registry `.mb/sources.json` | W01, W02 |
| Search over the raw layer, isolated from the wiki | a second corpus `.mb/sources_index/` | W03 |
| Provenance "note ← source" | frontmatter `sources: [<id>]` + `derived_notes` in the registry | W04 |
| Ingest: source → summary → edits to 10–15 linked pages | `mb ingest plan` (candidates) + the `mb-ingest` skill (edits) | W05, W09 |
| A parseable log of operations | `.mb/ingest-log.jsonl` | W06 |
| Contradiction lint | `mb curator contradictions` | W07 |
| "What to research next" | `mb curator gaps` | W08 |

**Deliberately not implemented:** `index.md`-first navigation — vector search
replaces it (`mb search`, ~20 ms warm); and a separate `log.md` — that role is
split between `changelog/` (for a human) and `.mb/*.jsonl` (for the machine),
see D3.

---

## 2. Invariants (checkable)

1. **Offline core.** No new module opens the network (D4). `mb ingest add`
   refuses `http(s)://` with exit 2.
2. **Report-only.** `ingest_plan.py`, `contradictions.py` and `gaps.py` hold no
   write function at all. `sources.py` writes ONLY under `.mb/` (D5).
   Checked statically: `tests/test_wiki_loop_report_only.py`.
3. **Write-once raw.** An existing `stored_path` is never overwritten (D6).
4. **Corpus isolation.** `curator`/`digest` read only `cfg.memory_index_dir` and
   physically cannot see the sources (D1/D2). Checked by a regression on
   byte-for-byte equality of their JSON.
5. **Atomicity.** Every state write goes through `config.atomic_write_json()`
   (`config.py:329`) or tmp+replace.
6. **`mb curator` with no subcommand does not change.** The new subcommands are
   optional (argparse subparsers are NOT `required`).

---

## 3. The `.mb/sources.json` registry contract

```json
{
  "schema_version": 1,
  "sources": {
    "<source_id>": {
      "title": "The source's title",
      "sha256": "<hex of the raw text>",
      "bytes": 12345,
      "chars": 6789,
      "origin": {"kind": "file|stdin|connector|external",
                 "ref": "<abs path|url|connector:doc>"},
      "stored_path": ".mb/sources/2026/slug--ab12cd34.md",
      "stored": true,
      "content_type": "text/markdown",
      "project": null,
      "tags": [],
      "ingested_at": "2026-07-28T18:40:00",
      "status": "new|partial|processed",
      "derived_notes": [],
      "supersedes": null,
      "superseded_by": null
    }
  }
}
```

* `source_id = sha256(normalize(text))[:12]` — content-addressed, so dedup is
  free. `normalize` is `capture.normalize_gist()` (`capture.py:25-29`:
  lowercase → strip punctuation → collapse whitespace), so "the same text with
  different spacing" does not produce a duplicate.
* The `sha256` in the record is the hash of the **raw** text (not the
  normalised one): it tells apart versions of one `origin.ref` that
  normalisation would collapse.
* `stored: false` — a retro-record from `mb ingest backfill` (W10.1): there is
  no text, only the fact that "this note was derived from here".
* Reading is **tolerant** (corrupt or missing → `{"sources": {}}`, like
  `digest._read_journal`, `digest.py:36-46`); writing is **strict**, through
  `atomic_write_json`.

### Storing the raw material

`.mb/sources/<YYYY>/<slug>--<id8>.md`:

```
---
type: source
source_id: ab12cd34ef56
title: "..."
origin: {kind: file, ref: "D:/docs/x.md"}
sha256: <hex>
ingested_at: 2026-07-28T18:40:00
immutable: true
---
<the text byte for byte as it is, with no markdown cleaning>
```

The separator between frontmatter and body is the standard `---\n`. The body is
NOT normalised: the raw material must be reproducible byte for byte.

### Slug sanitiser (Windows)

Forbidden characters `<>:"/\|?*` and control characters `\x00-\x1f` → `-`;
reserved names `CON PRN AUX NUL COM1..9 LPT1..9` (in any case, with any
extension) → an `_` prefix; trailing dots and spaces are trimmed (Windows drops
them silently); length ≤ 60 characters; NFC normalisation; non-Latin scripts are
allowed (the vault's titles are not all Latin); an empty result → `source`.

---

## 4. Map of integration with the existing code

### 4.1 `singularity/config.py`

| Point | What we do |
|---|---|
| `config.py:11-13` docstring «All runtime state lives inside the vault under `.mb/`» | **Fix it**: `.mb/sources/` is the only unrecoverable data (D1, R8) |
| `config.py:38-65` `DEFAULT_CONFIG` | +9 keys (see §6) |
| `config.py:93-165` `validate_config` | +type and range rules for the new keys |
| `config.py:162` `known = set(DEFAULT_CONFIG) \| …` | the new keys arrive automatically (they are in `DEFAULT_CONFIG`), so there will be no warnings |
| `config.py:168-177` `migrate_config` | bump `SCHEMA_VERSION` 1 → 2; never rewrite the user's values |
| `config.py:195-200` derived-paths | +`sources_dir`, `sources_index_dir` — no path joining at the call site |
| `config.py:244-246` `lock_dir` | becomes per-corpus (`lock_dir` stays as it is for `memory`, and `corpus_lock_dir(name)` is added) |
| `config.py:329-337` `atomic_write_json` | reused as it is |

### 4.2 `singularity/search/chunking.py`

| Point | What we do |
|---|---|
| `chunking.py:25` `CHUNK_SCHEMA_VERSION = 3` | **Do NOT touch.** The vault schema does not change → no full rebuild of the production index is needed (D2) |
| `chunking.py:180-246` `extract_chunks` | reused; for the sources corpus `type` is forced to `source` and the chunk gets `source_id` from the frontmatter — through a thin `sources_chunker` wrapper, so `extract_chunks` itself does not change |
| `chunking.py:249-254` `should_skip` | the proof of isolation: it cuts on the FIRST component, and `.mb` is in `skip_dirs` (`config.py:63`) |

### 4.3 `singularity/search/indexer.py` — the bulk of W03

| Point | Now | Becomes |
|---|---|---|
| `indexer.py:188-198` `_scan_vault(cfg)` | hardcoded `cfg.vault` + `should_skip(skip_dirs, skip_parts)` | `_scan_corpus(corpus)` — root and skip rules come from `Corpus` |
| `indexer.py:220-246` `rebuild`/`_rebuild` | hardcoded `cfg.memory_index_dir` | `rebuild(cfg, corpus=...)` |
| `indexer.py:264-326` `update`/`_update` | the same | `update(cfg, only_files, corpus=...)` |
| `indexer.py:153-180` `_writer_lock(cfg)` | one shared `cfg.lock_dir` | `_writer_lock(cfg, corpus)` — a per-corpus path; **the semantics of the cross-process mkdir lock, the takeover on `STALE_LOCK_SECONDS` and the rule that a losing contender NEVER removes somebody else's lock are all preserved unchanged** |
| `indexer.py:72-81` `index_files` | generic over `index_dir` | unchanged |
| `indexer.py:126-150` `_commit_generation` | generic over `index_dir` | unchanged → crash safety comes for free |
| `indexer.py:84-108` `load_committed_index` | generic | unchanged |
| `indexer.py:249-261` `inspect` | `cfg.memory_index_dir` | +an optional corpus |

`Corpus` — dataclass: `name`, `root`, `index_dir`, `skip_dirs`, `skip_parts`,
`chunker`. Two instances: `memory_corpus(cfg)` and `sources_corpus(cfg)`.
Backward compatibility: every public function keeps its current signature, with
`corpus=None` meaning the memory corpus.

### 4.4 `singularity/search/engine.py`

| Point | What we do |
|---|---|
| `engine.py:27-31` fields | +`src_embs`, `src_chunks` |
| `engine.py:53-56` `reload()` | +`self._load_one(cfg.sources_index_dir)`; the returned dict += `"sources"` |
| `engine.py:97-120` `_search_source` | unchanged (generic) |
| `engine.py:103-105` the filter whitelist | `has_filters` accounts for new keys if any appear |
| `engine.py:162-178` `search()` | the sources corpus takes part ONLY when `req["include_sources"]` is set |
| `engine.py:145-159` `_compact_line` | the corpus label: `item["source"] == "sources"` lands in `tag` when the chunk has no `type` — source chunks have `type` = `source`, so the label shows as `[source]` |

**The flag has to survive the whole path** (a missing link means the flag is
silently ignored):
`cli.py:47-63` (`--with-sources`) → `cli.py:224-239` (payload) →
`search/client.py:228-241` (`search`) and `client.py:215-225` (`_cold_search`) →
`search/daemon.py:44-48` (`cmd == "search"`) → `engine.search()`.
A test on every link, including the cold fallback (`tests/test_client_fallback.py`).

### 4.5 `singularity/curator.py` and `singularity/digest.py`

**Not touched at all** in W03: `curator._note_embeddings` (`curator.py:142`),
`curator._duplicate_pairs` (`curator.py:262`), `digest.plan` (`digest.py:151`)
read `cfg.memory_index_dir` and physically cannot see the second corpus. All
that is required is a regression test on byte-for-byte identical output.

The changes arrive later and narrowly:
* W04 — `curator.validate()` (`curator.py:100-127`): +2 `issues` keys
  (`source_not_in_registry`, `orphan_source`); the existing keys and the exit
  code do not change;
* W05 — lifting `_pool` (`digest.py:71-78`), the `_note_embeddings` mean-pool
  (`curator.py:134-157`) and `_blockwise_pairs` (`curator.py:227-252`) into
  `singularity/search/similarity.py`, under a regression test that the output
  did not change.

### 4.6 `singularity/nightly.py`

| Point | What we do |
|---|---|
| `nightly.py:175-184` `run()` | +the steps `ingest_index`, `gaps`, `ingest_pending`; each under the existing `_guard` (`nightly.py:54-58`) |
| `nightly.py:133-170` `_step_trend` | the trend line += `sources`, `unprocessed_sources`, `contradiction_candidates`, `open_questions` |
| `nightly.py:114-130` `_step_markers` | alongside it, retention of `.mb/ingest-log.jsonl` per `ingest_log_retention_days` |

Why `ingest_index` is a separate step: the watcher (`watch.py`) does NOT look at
the sources corpus — `.mb` is outside its scope (a direct consequence of D1).
Catching up is done by `mb ingest add` (through the warm daemon), `mb ingest
index` and nightly.

### 4.7 `singularity/hooks/vault_guard.py` + `singularity/vault_guard.py`

| Point | What we do |
|---|---|
| `vault_guard.py:25-27` `MANAGED_ROOTS`/`EXEMPT_ROOTS` | +`immutable_root(vault, abs_path, roots)` — a resolved path, so `..` traversal does not get through |
| `hooks/vault_guard.py:59-76` the branching | `immutable_root` is checked FIRST and returns `deny` **even under `vault_guard_mode: audit`** (D6) |
| `hooks/vault_guard.py:46` | `vault_guard_enabled: false` → still a complete no-op (documented honestly) |

### 4.8 `singularity/backup.py` — critical under D1

| Point | Now | Becomes |
|---|---|---|
| `backup.py:39-58` `create` | copies `config.json` only | +`.mb/sources.json` and all of `.mb/sources/**` with checksums in the manifest; above `backup_max_sources_mb`, a warning plus an explicit `--with-sources` |
| `backup.py:76-87` `verify` | verifies `files` | verifies the raw files too |
| `backup.py:90-119` `restore` | config + the generation pointer | +the registry and the raw layer; it **NEVER deletes existing files**, only adds the missing ones |

### 4.9 `singularity/connectors/base.py`

`NormalizedDoc` (`connectors/base.py:26`) is the existing contract for a
normalised document. Ingest accepts it as `origin.kind = "connector"`,
`origin.ref = "<connector>:<doc_id>"`. Connectors stay OFF by default; the core
works with `connectors: {}` (D4, [[06_Integrations_Layer]]).

### 4.10 `doctor.py`, `status.py`, `stats.py`, `hooks/session_start.py`

| File | What we add |
|---|---|
| `doctor.py` | a sources block: **FAIL** on "the registry is non-empty and the directory is missing" and "a registry entry with no file"; WARN on orphaned raw, a stale `sources_index`, a model mismatch between corpora, and `.mb/sources` under git-ignore. `--fix` does only the safe things and **never deletes raw** |
| `status.py:105-120` `snapshot` | +a `sources` section |
| `stats.py:16-61` `compute` | +`sources: {documents, chunks, indexed_at}` |
| `hooks/session_start.py` | a nag when `unprocessed_sources > 0`, rate-limited by the existing `nightly_nag_rate_limit_hours` (`config.py:57`), and never over the `session_context_max_lines` budget (`config.py:48`) |

---

## 5. The target flow

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
CLI-->A: {source_id, duplicate: false, stored_path, indexed: true|pending}
A->CLI: mb ingest index (if indexed=pending — catch up explicitly)
A->CLI: mb ingest plan <source_id> --json
CLI->IDX: read both committed generations (no model, no writes)
CLI-->A: {summary_target, touch[<=15], new_pages, conflicts}
A->U: takeaways + the list of pages to edit
U-->A: confirmation / correction of the list
A->W: summary note (frontmatter sources: [<id>])
A->W: edits to N existing notes (add-only, with a quotation)
A->CLI: mb ingest mark <id> --notes a.md,b.md --status processed
CLI->CLI: derived_notes + status + a line in .mb/ingest-log.jsonl
A->W: a line in changelog/YYYY-MM.md
A-->U: report: what was recorded, what stayed open
```

---

## 6. New config keys (schema_version 2)

| Key | Type | Default | Range | Meaning |
|---|---|---|---|---|
| `ingest_index_sources` | bool | `true` | — | kill switch for the second corpus |
| `ingest_max_source_chars` | int | `400000` | 1…10 000 000 | protection against junk; exceeding it is a refusal, truncation is forbidden |
| `ingest_fanout_top_k` | int | `15` | 1…100 | ceiling on `plan.touch` suggestions (D7) |
| `ingest_fanout_threshold` | float | `0.45` | 0.0…1.0 | minimum cosine for a candidate (symmetric with `digest_score_threshold`) |
| `ingest_log_retention_days` | int | `365` | 1…3650 | retention of `.mb/ingest-log.jsonl` |
| `immutable_roots` | list[str] | `[".mb/sources"]` | — | paths where the guard denies even in audit mode (D6) |
| `gap_link_min_inbound` | int | `2` | 1…100 | inbound-link threshold for `missing_pages` |
| `gap_learning_age_days` | int | `30` | 1…3650 | age of an undigested learning |
| `lint_llm_adjudication` | bool | `false` | — | batch adjudication through `mb llm` (optional) |
| `backup_max_sources_mb` | int | `200` | 1…100 000 | warning threshold when backing up raw |

---

## 7. Atomicity of the `.mb/ingest-log.jsonl` journal (decision W00)

**Chosen: a whole-file read-modify-write through tmp+replace**, the same trick as
`nightly._step_trend` (`nightly.py:147-169`).

The case against a single-line `O_APPEND`: `O_APPEND` is atomic only for writes
smaller than `PIPE_BUF`, and only on POSIX; on Windows the guarantees are weaker,
and a line carrying a long `notes` list easily exceeds the safe size. The file is
small (one line per ingest, 365 days of retention), so rewriting it whole is
cheaper than a class of hard-to-diagnose torn lines. The same trick is already
proven on `nightly-history.jsonl` and `reflect-history.jsonl`.

In addition: the write is serialised by the same per-corpus mkdir lock
(`ingest.lock`), so two parallel `mb ingest mark` runs cannot lose a line.

---

## 8. Order and releases

```text
W00 decisions + the design doc
 └─ W01 registry + write-once (.mb/sources/)
     ├─ W02 CLI mb ingest add|list|show|export
     │   └─ W03 the second corpus (Corpus + .mb/sources_index/)
     │       ├─ W05 mb ingest plan
     │       │   └─ W06 mb ingest mark + .mb/ingest-log.jsonl
     │       │       └─ W09 the mb-ingest skill
     │       ├─ W07 the contradiction detector
     │       └─ W08 mb curator gaps
     └─ W04 immutability + provenance
 W10 migrations    <- W01-W04
 W11 integration   <- W06, W08
 W12 E2E + release <- everything
```

R1 = W00–W04 · R2 = W05, W06, W09 · R3 = W07, W08 · R4 = W10–W12.
