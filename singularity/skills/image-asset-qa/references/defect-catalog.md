# Cutout/alpha defect catalogue — what they look like and how not to confuse them

The ground truth comes from the NIKA sessions (project `E:\Project\Content Creator website\`). The references below point at real crops, so you can calibrate your eye. Read this when interpreting `imgqa edges` / `backgrounds` / `detect-fringe`.

Root of the reference set: `E:\Project\Content Creator website\reports\asset-qa\`.
The original GT asset (SD3 remove-bg): `references\IMG GEN_SD3 - Remove Background_2026-07-13_05-34-49.png`.

---

## Real defects (flag these)

### fringe (a rim or soft shadow along the alpha edge)
A semi-transparent coloured halo or a residual shadow along the mask boundary — the background "leaked" into the feather zone. In NIKA it was a soft shadow ~80-150px at the boot of the seated hero (the reference `detect-fringe` positive, score ≈0.896, bbox inside the repair region `330,1620,680,428`).
- **Before:** `hero_boot_edge_black_200pct*.png`, `hero_boot_edge_white_200pct_lossless.png` — the fringe is visible on both dark and light backgrounds.
- **After defringe:** `hero_boot_edge_{black,rose,white}_AFTERFIX_200pct.png`, `..._AFTERFIX2_200pct.png`, `..._AFTERFIX3.png` — the series of repair iterations.
- Method: only from a lossless PNG crop, on 3 backgrounds. A fringe is often invisible on pink and screams on black (or the other way round).

### halo (a bright ring)
A bright ring along the edge (an over-exposed feather or an incorrect un-premultiply). The same pipeline as fringe: `detect-fringe` marks it `kind: "halo"`; the repair is `defringe` over the bbox.

---

## NOT defects (do not flag these — otherwise you get a false fail)

### hair matting (fine semi-transparent strands)
Fine hairs with partial alpha are a CORRECT cutout, not a fringe. `detect-fringe` must stay silent on them (GT-N2: 0 detections on the hair crop of the same original). A global threshold would have destroyed them — which is why `defringe` is param-driven over narrow bboxes only.
- Example: `hero_hair_{black,rose,white}_200pct.jpg` — that is what normal matting looks like; leave it alone.

---

## "Wrong test method" cases (a defect of the method, not of the asset)

Before calling something a defect, check whether the way you are checking is itself broken.

### `<img src=svg>` does not inherit `currentColor`
An icon "in the wrong colour" in a preview is a **defect of the preview method**, not of the icon. An external `<img>` or `background:url(svg)` does not inherit the parent's `currentColor`; for the colour to apply, the SVG has to be inlined (inline-inject). Do not raise a bug against the icon — fix the way it is rendered. (NIKA rule e / [[WSF-00]].)

### A JPEG preview exaggerates an alpha defect (council H10)
An edge cannot be judged from a `.jpg` composite: JPEG artefacts fabricate a fringe that is not in the lossless source. The overview files `hero_{black,rose,white}_full.jpg` / `..._FINAL_full.jpg` are good only for a rough "broadly fine"; any decision about the edge comes from the PNG (which is exactly why `edges` writes lossless PNG). If a "defect" is visible on the jpg and disappears on the png crop, it is a jpg artefact, not the asset.

### A baked checkerboard is not transparency
A 24bpp image with a grey-and-white checkerboard painted into it looks "transparent" but has no alpha channel. `probe` tells them apart categorically (`baked_checkerboard`); the eye does not. Always `probe` first.
