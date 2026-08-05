"""design-scout — deterministic design-reference collector (WSF-09).

Typed CLI over the source registry (sources.json): probe source health,
collect references for a set of queries into a provenance manifest and an
HTML moodboard for owner acceptance. Contract: singularity/scout/CONTRACT.md
(schema_version 1) — license hybrid per OD-2, budgets and retry-by-error-class
per §5, per-source typed statuses per §6.

Stdlib only by design: no new engine dependencies, network via urllib with an
honest User-Agent. Untrusted web content is never executed — only parsed for
metadata (titles, links, thumbnail URLs).
"""
__version__ = "0.1.0"

# Declared here, not in cli.py: `_contract` needs it and `cli` imports
# `_contract`, so keeping it in the CLI made the two import each other. The
# package is the one place both can reach without a cycle.
SCHEMA_VERSION = 1
