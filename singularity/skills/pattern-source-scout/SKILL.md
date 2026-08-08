---
name: pattern-source-scout
description: Brief → a reproducible shortlist of installable motion/3D/component patterns from external ecosystems (21st, React Bits, three.js examples, Motion Primitives, GSAP) with licences, a score out of 100 and an HTML review board for the owner's acceptance. A thin wrapper over the `pattern-scout` CLI. Use when "find components", "what ready-made patterns exist", pattern shortlist, "installable examples for the brief", selecting motion/3D candidates before adaptation/intake.
---

# pattern-source-scout — brief → a shortlist of candidates

All the deterministic work lives in the `pattern-scout` CLI (the engine, contract `singularity/providers/CONTRACT.md`, provider-catalog/1). The skill builds the queries, interprets scores and hard fails, and carries the review board to the owner gate. **Code that is found is NEVER executed at this stage** — installation happens only through quarantine intake (E03).

> **Zero-code rule:** search, normalisation, cache, score and board are `pattern-scout` subcommands. A new source is a line in registry.json. 0 new scripts.

## The order

```bash
pattern-scout probe --json     # provider health (evaluative: >=1 live)

pattern-scout search \
  --query "code rain" --query "terminal background" --query "scroll narrative" \
  --capability particles --capability scroll-linked \
  --out "<evidence dir OUTSIDE the project>" --board --profile static-lite
# PSCOUT_PROTECTED_ROOTS=<project> forbids writing into the project.
# -> candidates.json (typed) + review-board.html (scored, for the owner)
```

## Query expansion (the LLM part)

From the brief or moodboard, produce 3-6 short queries: a device ("code rain"), a surface ("terminal background"), a pattern ("scroll narrative"). The ecosystems are English-language, so write the queries in English regardless of the language of the brief. The capability filter comes from the taxonomy in contract §3 — a typo in a capability gives a loud exit 2, not an empty result.

## How to read the output

- `importable=false` plus `import_block_reason` is the hard gate of §2: `license unknown` (all of 21st!), paid (report-only), reference-only, remote-code. This is NOT a defect of the candidate — it is the status "cannot be pulled in without a separate check".
- `stale=true` means a candidate came from the cache while the provider was unreachable; re-check it live before shortlisting.
- The score out of 100 is only for sorting the shortlist (the components and their evidence are in the card's details): brief-fit 25 / license 20 / stack 15 / perf 15 / deps 10 / a11y 10 / freshness 5. Anything unknown gets a neutral default, never the maximum — a candidate must not win on missing data.
- `providers` statuses: `unavailable:*+cache` means the provider is down and the output came from the cache; the run is still valid.

## Gates

- [ ] No installing or executing candidates — metadata and the board only. Installation is E03 quarantine.
- [ ] `license=unknown` is never marked importable under any circumstances (the contract enforces it, and the skill does not route around it).
- [ ] **The licence is read BEFORE the candidate is named in a recommendation, not before vendoring.** By the time of the check the decision is already made, and withdrawing a recommendation costs more than not giving it. The licence field on GitHub is the result of auto-detection: `Other`/`NOASSERTION` means "GitHub did not recognise it", not "there is no licence", and requires opening `LICENSE` itself and reading the text for commercial use, disclosure, a sponsorship requirement, and virality (GPL/AGPL).
- [ ] The shortlist and art direction are confirmed by the owner from review-board.html (human gate §12) — the board is not approval by itself.
- [ ] The run (queries, statuses, counters, the board's path) goes into the learnings via `mb learn`.
