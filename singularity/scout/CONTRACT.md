# design-scout — typed CLI contract (schema_version 1)

Deterministic collector of design references from the open-source registry
`sources.json`. Versioned WITH the code: a change here is a change of the tool.
Owner-ratified inputs: OD-2 (license hybrid, 2026-07-14), WSF-09 probe-spike
(2026-07-18).

## §1 Envelope

Every `--json` run prints exactly one JSON object:

```json
{
  "schema_version": 1,
  "command": "sources|probe|collect|selftest",
  "status": "ok|error",
  "tool": {"design-scout": "<version>", "python": "<ver>"},
  "inputs": {...},
  "data": {...},
  "verdict": {"result": "PASS|FAIL", "reason": "..."},
  "outputs": ["..."],
  "error": "..."
}
```

`tool` versions are evidence and must never crash the envelope.
Consumers pin `schema_version == 1` and fail loudly on anything else.

## §2 Commands and exit semantics

| Command | Class | exit 0 | exit 1 | exit 2 |
|---|---|---|---|---|
| `sources` | informational | printed | — | tool error |
| `probe` | evaluative | floor met | floor NOT met | tool error |
| `collect` | generating | run completed (per-source statuses inside) | — | tool error |
| `selftest` | evaluative | all checks green | check failed | tool error |

- exit 2 = the TOOL did not work (bad registry, unwritable --out, contract
  violation). It is never a domain verdict and never means "no results".
- `collect` completing with some sources `unavailable` is exit 0 by design:
  one dead provider must not kill the run (WSF-E00 principle). The per-source
  status table in `data.sources` is the truth.
- Probe floor (owner pre-mortem): >= `budgets.floor_live_total` live sources
  AND >= `budgets.floor_live_api` of them API-backed.

## §3 Filesystem policy

- `collect` requires `--out`; the directory is canonicalized and created.
- Writes are refused into: the engine repo itself, any root listed in
  `DSCOUT_PROTECTED_ROOTS` (os.pathsep-separated), or `--out` equal to /
  above any of them. Calling skills set this to the target project tree —
  the tool physically cannot write into an audited project.
- All file writes are atomic (temp + `os.replace`).
- Deterministic outputs inside `--out`: `manifest.json`, `moodboard.html`,
  `thumbs/<id>.<ext>` (only for `api-free-license` records).

## §4 License policy (OD-2, owner 2026-07-14 — hybrid)

Per-source `license_policy` is enforced by construction:

| policy | download | embed remote | record license |
|---|---|---|---|
| `api-free-license` (Openverse, Pexels) | thumbnails YES | yes | per-item, REQUIRED — item without a license field is dropped, never "unknown-but-downloaded" |
| `api-hotlink` (Unsplash ToS: hotlinking required) | NO | yes (their URLs) | platform license per-item |
| `links-only` (Land-book, Httpster, Minimal.gallery, Godly, SiteInspire) | NO — browsing is not a storage right | thumbnail URL embed only | source-level note |
| `per-block` (Are.na) | NO by default | yes | per-block when present, else `unknown` + link-only |

A `links-only`/`per-block`/`api-hotlink` record can NEVER produce a file in
`thumbs/` — violation is a bug, guarded by tests.

URL hygiene: `page_url`/`media_url` pass an http(s) scheme allowlist; a record
without a valid http(s) `page_url` is dropped at creation (a `javascript:`
href would execute on owner click in the moodboard; empty page_urls collapse
into one id and corrupt dedup evidence).

## §5 Budgets (registry `budgets`, logged per run in `data.budget_log`)

- `per_fetch_timeout_s` (default 15) — every network call;
- `max_results_per_source` (default 24), `max_thumb_downloads` (default 60);
- `max_sources_per_run` (default 8);
- `wall_budget_s` (default 300) — soft stop: no NEW fetches after exceeded,
  logged as `stop_condition: wall_budget`;
- retry BY ERROR CLASS only: `timeout` and `http-5xx` retry once;
  `http-403`, `http-404`, `http-429`, `dns`, `ssl`, `too-large` NEVER retried
  (anti-bot/absent/rate-limit — a retry is noise or burns quota), recorded as
  typed per-source status;
- response size guard: bodies over 10 MB are refused as class `too-large`
  (never buffered further, never written).

## §6 Per-source statuses (typed, in `data.sources`)

`live` | `unavailable:<class>` | `skipped:no-key` | `skipped:disabled` |
`skipped:budget`. `<class>` is a §5 fetch class plus two adapter-level
classes: `bad-json` (unparseable body) and `adapter-error` (provider schema
drift crashed the adapter — the run continues, §2). A wall-budget stop hit
mid-source reports as `skipped:budget`. Raw exceptions never escape.

## §7 Robots / access policy

Sources whose robots.txt disallows AI crawlers (Godly, SiteInspire —
probe-spike 2026-07-18) ship `enabled: false` with `disabled_reason`;
enabling them is an OWNER decision recorded in the registry, not a code
change. The fetcher sends an honest UA (`SingularityDesignScout/<ver>`).

## §8 Determinism & provenance

- Every record carries: source, method, query, retrieved_at (UTC ISO-8601),
  endpoint, http_status, license, license_policy, page_url.
- Dedup key: normalized `page_url` (lowercased host, stripped query/fragment,
  trailing slash); first occurrence wins; drops are counted in
  `data.dedup_dropped`.
- Ordering in manifest/moodboard: registry order, then source result order —
  two runs over identical responses produce identical manifests except
  `retrieved_at`.

## §9 Amendments

- 2026-07-18 (pre-merge review, schema unchanged): per-source adapter
  isolation (`unavailable:adapter-error`), http(s) scheme allowlist + drop of
  empty `page_url`, `http-429`/`too-large` classes, 10 MB response guard,
  registry validation (source name charset `[a-z0-9-]+`, `probe_url` required
  for enabled sources), probe floor logic offline-testable via injectable
  transport, `--queries-file` read as utf-8-sig.

Changes require an owner-visible commit touching this file.
