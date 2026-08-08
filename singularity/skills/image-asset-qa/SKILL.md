---
name: image-asset-qa
description: QA of cut-out (cutout/alpha) images before they go on a site — is the transparency real, is there fringe/halo along the edges, are the edges correct on 3 backgrounds. A thin wrapper over the deterministic `imgqa` CLI. Use when checking a PNG asset with transparency, "check the cutout edges", "is there a halo or fringe", "asset QA", asset QA, alpha/cutout review, before handing a hero/product PNG to the markup.
---

# image-asset-qa — acceptance of cutout/alpha assets

All the deterministic work lives in the `imgqa` CLI (the Singularity engine, contract `singularity/imgqa/CONTRACT.md`, **schema_version 2**). The skill holds no logic: it says WHEN to apply it, HOW to read the output, WHAT counts as a defect, and WHERE the human gate is.

> **Zero-code rule (WSF-13 trajectory invariant):** if your hand reaches for a `*.py`/`*.mjs` to "read/compare/render an image" — STOP. All of it already exists as an `imgqa` subcommand. A QA session must create **0 new scripts**. A new script is a regression, not a solution.

## When to apply it

- A cut-out asset has arrived (a PNG with transparency): hero, product, character — before it goes into the markup.
- You need to decide "the edge is clean / there is a fringe / it needs a defringe".
- Checking "is this real alpha or a baked-in checkerboard?" (in NIKA a 24bpp checkerboard was taken for transparency — `probe` catches this categorically).

Not for: WCAG text contrast as a task of its own (that is `contrast` / the contrast-and-a11y-check skill), background removal (that is [[WSF-03]] `cutout`/bg-remove), video (video-visual-review).

## Hard gates (do not skip)

- [ ] **QA only on lossless (PNG).** Never judge an edge or alpha from a JPEG — JPEG compression fabricates and exaggerates fringing (lesson NIKA-02, council H10). Overview composites `*_full.jpg` are good only for a rough look; the edge decision comes from the PNG crop.
- [ ] **`probe` FIRST.** Until the alpha is confirmed real (`has_alpha && !baked_checkerboard`), everything else is meaningless.
- [ ] **3 backgrounds are mandatory:** black, pink, white. A fringe invisible on one background shows up on another. One background is an incomplete acceptance.
- [ ] **200% zoom** for inspecting edges (`edges` renders exactly that way). At 100% a defect of 80-150px does not read.
- [ ] **`defringe` is param-driven only:** the bboxes come from `detect-fringe` (or explicitly from a human), **never** a global threshold — a global threshold would have killed the fine hair matting (see defect-catalog, case GT-N2). The bbox must be generous (≥3× feather), otherwise the mask edge is half-repaired and re-detected.
- [ ] **Final acceptance of the edge belongs to a human.** `detect-fringe` is DETECTION, not acceptance. The LLM or the human remains the last gate on "the edge is fine".

## The order (acceptance pipeline)

```bash
# 1. Is the transparency real?  (evaluative)
imgqa probe ASSET.png --json
#    verdict.result != PASS  -> this is not usable alpha (baked checkerboard / no alpha). Going further is pointless.

# 2. Inspection on 3 backgrounds + edges at 200%  (informational, writes files)
imgqa backgrounds ASSET.png --out OUTDIR      # black / pink / white
imgqa edges       ASSET.png --out OUTDIR       # lossless 200% crops of the alpha boundary
#    -> read the generated PNGs visually (multimodal Read), check against references/defect-catalog.md

# 3. Deterministic fringe detection  (evaluative -> bboxes)
imgqa detect-fringe ASSET.png --json
#    verdict PASS = no fringe.  FAIL -> data.detections[].bbox are repair candidates.

# 4. A human looks at the bboxes; on confirmation, a local repair  (transform)
imgqa defringe ASSET.png --bbox X,Y,W,H --out OUTDIR   # bbox from step 3, generous

# 5. Confirm it came out clean
imgqa detect-fringe ASSET.png_fixed.png --json         # expect 0 detections
#    + the final human gate on the edge across 3 backgrounds
```

## How to read the output (exit semantics, contract §2)

Every call with `--json` yields an envelope with `schema_version`, `tool` (Pillow/numpy/scipy versions) and, for evaluative commands, `verdict`. **Pin `schema_version == 2`; on an unknown version fail loudly rather than guess.**

| Class | exit 0 | exit 1 | exit 2 |
|---|---|---|---|
| evaluative (`probe`, `compare`, `contrast`, `detect-fringe`) | verdict **PASS** | verdict **FAIL / defects found** | **tool error** |
| informational (`edges`, `backgrounds`, `selftest`) | it ran | — never happens — | tool error |
| transform (`defringe`, `sheet`) | it ran | — never happens — | tool error |

- ⚠️ **exit 2 ≠ "no defects".** exit 2 means "the tool did NOT run" (a corrupt file, a bbox outside the image, a decompression bomb, a missing optional dependency). Never read it as a pass. Telling "clean" apart from "did not run" is mandatory.
- Read the verdict from the typed `verdict.result` field; the exit code is only a mirror for shell gating.
- A `detect-fringe` FAIL is not by itself a sentence on the asset: it is a list of candidates for a human, not an auto-fix.

## The defect catalogue

What fringe/halo/hair-matting look like, with example links to real NIKA crops, plus the "wrong test method" cases (svg currentColor, JPEG previews) — in [`references/defect-catalog.md`](references/defect-catalog.md). Check against it when interpreting `edges`/`backgrounds`.

## Recording the result

The QA verdict (defects found, bboxes, what was fixed) goes into the task's development history (vault learnings via `mb learn`), as contract §2 requires. A defect on a dogfood asset becomes a small compressed crop in git as a versioned benchmark (rule §9 of the contract).
