# static-lite starter (WEBSITE-FACTORY WSF-10)

The starter for the `static-lite` profile (router E01): a content site whose
effects are isolated vanilla islands only. No framework runtime.

## Baked-in conventions

- **tokens-first**: colours, fonts and motion numbers live only in
  `css/tokens.css`; modules read them through `getComputedStyle`.
- **cascade layers**: `reset < primitives < layout` — additions go in their own layer.
- **modules**: `js/modules/<name>.js` → `init<Name>()` → `[data-module]` →
  `safeInit` in `js/main.js`. A module failing does not take the site down.
- **visual-test contract**: `?visual-test=1` silences animation; every canvas
  draws one seeded frame (mulberry32). Determinism is verified by the harness.
- **reduced-motion**: the site-wide kill switch is already in `css/main.css`.
- **truth-gate**: `__PLACEHOLDER__` values are NOT facts; they are replaced only
  with the owner's data.

## Rolling out a new project

1. Copy the directory, replace `__PROJECT_NAME__` and the placeholders (owner).
2. Fill in `.factory/manifest.json` honestly; `stackrouter` confirms the profile.
3. `npm install` → `npm run serve`.
4. The **visual-regression-bootstrap** skill — the harness in one command
   (only the owner approves a baseline).
5. Patterns come ONLY from the golden registry (`golden search` → `golden install
   --dest <project>`) or through the full scout→intake→recipe path.
6. Quality gates before any merge: determinism STABLE, motion-qa,
   a11y-audit, fidelity-audit.
