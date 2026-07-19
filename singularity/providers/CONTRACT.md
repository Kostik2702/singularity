# pattern-scout — provider layer contract (schema provider-catalog/1, WSF-E00)

Typed provider layer for installable motion/3D/component patterns. Canonical
concept: decision WEBSITE-FACTORY-V2-ECOSYSTEM §3-§5. A provider is an
ADAPTER behind this contract, never a hard dependency of the orchestrator.

## §1 Normalized candidate

Every search result from every provider is reduced to this record; nothing
provider-specific leaks upward:

```json
{
  "candidate_id": "provider/native-id",
  "provider": "twentyfirst",
  "kind": "component|section|motion-recipe|shader|scene|template|video-composition|example",
  "name": "...", "description": "...",
  "source_url": "https://...",
  "preview_url": "https://... | null",
  "retrieved_at": "ISO-8601",
  "source_revision": "tag|commit|content-hash|null",
  "license": "SPDX id | custom | unknown",
  "usage_rights": "commercial|personal|unknown",
  "price_tier": "free|entitled|paid",
  "framework": "static|react|next|three|r3f|remotion|unknown",
  "install_method": "source|shadcn|cli|npm|reference-only",
  "install_command": "... | null",
  "dependencies": ["..."],
  "capabilities": ["hero", "scroll-linked", ...],
  "risks": ["..."],
  "health": "live|cached|unavailable",
  "stale": false,
  "importable": false,
  "import_block_reason": "license unknown | paid: report-only | ... | null",
  "evidence": ["..."]
}
```

Validation is code (`candidate.py`), not prose: a record failing validation
is a provider-adapter bug and never reaches consumers.

## §2 Import gate (hard rules, decision §4)

`importable=true` requires ALL of: license is neither `unknown` nor empty;
`price_tier=free` or explicitly entitled (paid is REPORT-ONLY until the
owner gate); `install_method != reference-only`; no `remote-code` risk.
The gate is computed at normalization — an adapter cannot override it.

## §3 Capability taxonomy (decision §5)

`hero, scroll-linked, shader, camera-path, particles, text, transition,
interaction, media, background, cursor, 3d, fallback`. Search targets
capabilities and free text, never brand names. Unknown capability strings in
a query are a tool error (typo protection), unknown in a RECORD are dropped
with a warning.

## §4 Adapter contract

Each adapter implements: `health(ctx)`, `search(ctx, queries, capabilities)`.
(`get`/full source retrieval belongs to E03 quarantine intake — E00 exposes
`install_command`/`source_url` references only.) Adapters receive a Fetcher
(scout §5 semantics: per-fetch timeout, retry ONLY by error class, wall
budget) and must never raise: failures map to typed statuses
`live | unavailable:<class> | skipped:disabled | skipped:no-key |
skipped:budget` (scout §6 vocabulary + `unavailable:adapter-error` isolation
wrapper at the runner level). One dead provider never kills a run.

## §5 Cache + offline (stale marker)

Successful searches are cached per `cache_key = sha1(provider + normalized
query + capabilities)` under `PSCOUT_CACHE_DIR` (default
`~/.singularity/provider-cache`), storing retrieved_at + candidates. When a
provider is unavailable, the runner serves the cached entry with every
candidate marked `health=cached, stale=true` and the provider status
`unavailable:<class>+cache`. Cache is evidence, not truth: stale candidates
keep their ORIGINAL retrieved_at. No TTL eviction in v1 (explicit
`--no-cache` bypasses reads; writes always happen).

## §6 Budgets

Registry `budgets`: `per_fetch_timeout_s`, `retry_max`, `retryable_classes`,
`wall_budget_s`, `max_candidates_per_provider`, `max_providers_per_run`,
`cli_timeout_s` (subprocess adapters, e.g. 21st CLI). Logged per run.

## §7 Paid policy (owner-ratified 2026-07-17)

free/open/current-entitlement providers enabled by default; anything
`price_tier=paid` is listed report-only (`importable=false`,
`import_block_reason="paid: report-only until owner gate"`). Enabling a paid
source is an owner decision recorded in the registry.

## §8 CLI exit semantics (mirror scout §2)

| command | class | exit 0 | exit 1 | exit 2 |
|---|---|---|---|---|
| `providers` | informational | printed | — | tool error |
| `probe` | evaluative | ≥1 provider live | none live | tool error |
| `search` | generating | run completed (typed per-provider statuses) | — | tool error |
| `selftest` | evaluative | all checks green | check failed | tool error |

## §9 Amendments

None. Changes require an owner-visible commit touching this file.
