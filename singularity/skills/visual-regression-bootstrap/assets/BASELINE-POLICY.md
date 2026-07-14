# Baseline policy (visual regression)

Deployed by the visual-regression-bootstrap skill. This file is LAW for this
project's visual baselines.

1. **Baselines are updated ONLY by the owner's explicit decision**, via
   `npm run test:update-baseline`, after the owner has reviewed the new
   screenshots. Never silently. Never "to make the suite green". A red
   visual test after an intentional redesign is CORRECT behavior — it stays
   red until the owner accepts the new look (NIKA precedent: 9 deliberately
   red baselines awaiting owner review).
2. **Determinism before baselines.** `npm run check:visual-determinism` must
   print STABLE for every viewport before any baseline is generated or
   updated. UNSTABLE = fix the page's ?visual-test=1 mode, don't retry until
   lucky.
3. **The tolerance must stay proven.** maxDiffPixelRatio (0.0015) was chosen
   by a live negative test: force a real visible change (e.g. an accent color
   swap), the suite MUST go red; revert, it must go green. Any change to the
   tolerance or the spec re-runs this negative test. A suite that has never
   failed proves nothing.
4. **Dogfood regressions feed back.** Every real visual defect that the suite
   caught — or MISSED — becomes a recorded case: what changed, what the diff
   ratio was, whether the tolerance held (imgqa CONTRACT §9 spirit).
