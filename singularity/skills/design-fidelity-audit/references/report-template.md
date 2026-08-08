# design-fidelity-audit report template

An example of a real report in this format: NIKA `reports/NIKA-VISUAL-AUDIT-2026-07-14.md` (19 tasks VIS-00..18, six delivery slices). The report goes into `reports/` of the TARGET workflow (not into the audited tree; if the project is read-only, into the audit's out directory instead).

```markdown
# <PROJECT> — visual fidelity audit <YYYY-MM-DD>

## Verdict
<1-3 sentences: the scale of the divergence, the main cause, the recommendation.>
Evidence: fidelity-metrics.json (<path>), screenshots <viewports>, mockup <path>.

## Numbers
| Metric | Mockup | 1440 | 768 | 390 |
|---|---|---|---|---|
| height (px) | ... | ... | ... | ... |
| proportion (h/w) | ... | ... | ... | ... |
| hero height | ... | ... | ... | ... |
| overflow / consoleErrors | — | ... | ... | ... |

## Category A — to fix (fix list)
| # | Section/selector | Observation (number/screenshot) | Expectation from the mockup | Slice |
|---|---|---|---|---|
| A-01 | #hero | height 1013px > viewport 900 | the first screen in full | S1 |

## Category B — deliberately different (do NOT fix)
| # | Divergence | Grounds (decision/link) |
|---|---|---|
| B-01 | no pricing block | truth policy, decisions/<...> |

## Open questions for the owner
<Divergences for which neither a decision in the vault nor an obvious cause was found.>

## Forbidden shortcuts
<What must NOT be done while fixing: breaking truth gates, copying unverified mockup content, global hacks.>
```

Rules for filling it in:
- Every category A row carries a number or a screenshot reference; "it looks wrong" without evidence is not accepted.
- A category B entry never exists without a link to a decision — no link means it is an "Open question", not a B.
- The Slice column groups fixes into coherent packages of work (the order of rollout).
