---
name: motion-3d-implementer
description: Implementation of a motion/3D slice from an accepted adaptation recipe (or an explicitly marked greenfield spike) — picking the cheapest sufficient runtime (CSS → Web Animations → Canvas2D → GSAP → R3F → prerendered media), a single motion grammar, lifecycle hygiene, progressive enhancement; every implementation is closed by motion-qa + determinism + fidelity evidence. Use when "implement the section/effect", implement slice, motion/3D markup from a recipe, after pattern-adapter and before visual acceptance.
---

# motion-3d-implementer — a coherent scene, not a collection of effects

The input is strictly one of two: a **valid recipe** (`component-intake recipe --validate` exit 0) or a **greenfield spike explicitly marked** in the learnings. Free-form "make it pretty" is not an input.

> **Zero-code QA rule:** checks come only from the existing gates (motion-qa, check-visual-determinism, a11y-audit, fidelity-audit, imgqa). 0 new scripts per implementation.

## 1. Choosing the runtime — the cheapest sufficient one (decision §8)

Descend from the top; every step down needs a reason recorded in the learnings:

1. **CSS / Web Animations** — reveal/hover/state transitions.
2. **Canvas2D island** — dense procedural 2D (rain, glitch wall). GT precedent: the letter-glitch port was Canvas2D, 3KB, instead of a 302KB React path (spike E01).
3. **GSAP** — complex timeline/pin/scrub (only with real orchestration).
4. **R3F/three** — only when the brief calls for a scene graph, camera or materials; always a lazy island plus a static fallback (router constraints S3).
5. **Prerendered media** — when interactivity is not needed and real-time costs more.

The project profile (stackrouter) is the ceiling: React does not appear in static-lite for the sake of an effect.

## 2. Mandatory properties of the implementation (the viewer's invariants)

- **Motion grammar:** durations, easings and ticks come from the project's tokens or the recipe's numbers; values picked by eye are a defect.
- **Determinism:** `?visual-test=1` yields a byte-stable frame (seeded PRNG, animation off) — the WSF-08 harness contract.
- **Lifecycle:** destroy() with cancelAnimationFrame + removeEventListener + observer.disconnect; pause outside the viewport (IntersectionObserver). motion-qa checks this with `raf-after-settle`.
- **Reduced-motion:** a static, meaningful frame plus a live reaction to the media query changing; a site-wide CSS kill switch is mandatory.
- **No-JS:** the section's meaning and CTA live in the markup, not in the canvas.
- **Wall-time, not frames:** everything "over time" (particle decay, fade, speed) is computed through `delta = time - lastTime` with a clamp of ~100 ms, and movement is normalised as `pos += v * (delta/16.7)`. rAF does not guarantee 60fps: under throttling (a headless run, a weak machine, power saving) "0.5 s at 60fps" turns into 5-10 real seconds — the decor hangs and e2e tests flake. The tell-tale: a constant like `decay = 1/40` "per frame", or `alpha += STEP` with no delta; the test is stable in isolation and flakes in a full run.
- **An icon that must be coloured by its context is injected as an inline `<svg>`:** `<img src="icon.svg">` does not inherit `currentColor` or the page's CSS variables. Do it systematically (an injector module plus a `data-inline-icon` marker), not case by case; otherwise icons turn black on coloured backgrounds.
- **Licence attribution** goes in the code header when the recipe requires it.

## 3. The order

```bash
# 0. The recipe is valid (or the spike is marked)
component-intake recipe --validate <recipe.json>          # exit 0 is mandatory
# 1. Implementation in the project's feature branch, per the recipe's patch_set
# 2. The evidence cycle (every round, cap 3 iterations -> escalate to the owner):
npm run check:visual-determinism                          # STABLE is mandatory
node .../motion-qa.mjs --project <proj> --url <live> --budgets <proj budgets>
node .../fidelity-audit.mjs ... (a screenshot for acceptance)
# 3. STOP before the baseline: visual acceptance and the baseline belong to the owner alone (GATE-5)
```

## GT precedent (the reference execution)

letter-glitch-wall in matrix-3d-web (2026-07-19): from an accepted recipe, a Canvas2D island, every §2 invariant met, motion-qa PASS 9/9, determinism STABLE ×3, and two gaps caught by gates fixed in the same cycle. An implementation must hold up to that same quality of evidence.
