---
name: component-intake
description: Quarantine of an external component before it reaches the project in any form — fetch without execution, sha256 provenance, a licence classifier, a ban on lifecycle scripts, a scan for hidden network/eval, a dependency review, an esbuild check, and a PASS/ADAPT/REJECT verdict. A thin wrapper over the `component-intake` CLI. Use when "check this component", "can we take this component", intake, quarantine, before installing or adapting any external code from a shortlist.
---

# component-intake — quarantine before adaptation

The `component-intake` CLI (the engine, contract `singularity/intake/CONTRACT.md`, component-intake/1). **v1 is inspect-first**: a candidate's code is NEVER executed, lifecycle scripts are always denied, and npm install does not exist as a class. The verdict ceiling in v1 is ADAPT (the runtime phases are typed unavailable; unavailable ≠ PASS).

> **Zero-code rule:** fetch, hash, scan and verdict are the `run` subcommand. 0 new scripts.

## The order

```bash
# always through the E02 provenance chain (the licence comes from the provider registry):
INTAKE_PROTECTED_ROOTS=<project> component-intake run \
  --candidates <candidates.json from pattern-scout> --id <candidate_id> \
  --workspace <disposable dir OUTSIDE the project> --json
# exit 0 = PASS (unreachable in v1), 1 = ADAPT|REJECT (read verdict.result), 2 = tool error
```

## How to read the verdict

- **REJECT** — do not adapt: license unknown / lifecycle scripts / hidden network / eval / build failure. The reasons are in `data.reject_reasons`. A rejected candidate is not a defect of the pipeline.
- **ADAPT** — it may enter the implementation branch ONLY through an adaptation recipe (E04); `data.requirements` is a mandatory input to that recipe (a reduced-motion variant, a lazy island for heavy deps, runtime evidence v2).
- `checks.build.status=unavailable` — point `INTAKE_ESBUILD` at the esbuild.exe of any vite project; unavailable caps the verdict, it does not wave it through.
- `data.checks.snapshot` (sha256 + origin + retrieved_at) is the provenance, and it must reach the recipe and the golden registry.

## Gates

- [ ] A candidate with no entry in candidates.json (no provenance chain) counts as license unknown and therefore REJECT — never "helpfully" supply a licence by hand without checking the source.
- [ ] The classifier reads the `LICENSE` file, not GitHub's field: `Other`/`NOASSERTION` means "auto-detection did not recognise it", not "there is no licence". If somebody already recommended the candidate without reading the text, that recommendation is not provenance.
- [ ] `INTAKE_PROTECTED_ROOTS` is ALWAYS set to the target project.
- [ ] A REJECT is not disputed by the skill; contested cases go to the owner with evidence.
- [ ] The verdict and requirements go into the learnings (`mb learn`); intake-report.json is the evidence for E04.
