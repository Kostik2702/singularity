---
name: bg-remove
description: Background removal from an image with automatic edge QA — rembg/BiRefNet in an isolated ML venv through imgqa cutout, then probe/edges/backgrounds/detect-fringe and, if needed, a local defringe. Use when "remove the background", "make a PNG with transparency", "cut out the object/person", background removal, cutout, "I need a transparent background", preparing a hero/product asset from a photo.
---

# bg-remove — background cutout with a QA pipeline

All the work goes through `imgqa` commands (contract schema 2). The ML engine (rembg + BiRefNet, ~930MB of weights) lives in the isolated venv `D:\venvs\imgqa-ml` (OD-1) — the Singularity engine never imports it, only dispatches to it as a subprocess.

> **Zero-code rule:** cutting out, QA and repair go only through `imgqa` subcommands. No hand-written rembg scripts.

## What the model can and cannot do (from the 2026-07-14 spike)

- **The target case is photographic subjects:** people, objects. Hair and strands come out well (verified on a video frame with loose hair and white clothing against a light background); ~9s per image on CPU.
- **Line art and logos on a flat background** — works (a wedding invitation was cut out cleanly).
- **NOT the target case:** pages with text or graphics on white — BiRefNet is a salient-object model, and text gets partially eaten. For such inputs a cutout makes no sense — say so to the user instead of driving the pipeline.

## The pipeline

```bash
# 0. (once per machine) the ML venv must exist: D:\venvs\imgqa-ml
#    exit 2 with a remediation means there is no venv; create it per the instruction in the error. Weights live on D:.

# 1. The cutout (dispatch, ~15s cold start)
imgqa cutout PHOTO.png --out OUTDIR --json
#    exit 2 = the tool did not run (no venv / no dependencies / corrupt input) — NOT "the background is already clean"

# 2. Automatic QA of the result (the image-asset-qa skill, steps 1-3):
imgqa probe OUTDIR/PHOTO.cutout.png --json           # is the alpha real?
imgqa backgrounds OUTDIR/PHOTO.cutout.png --out QADIR # 3 backgrounds
imgqa edges OUTDIR/PHOTO.cutout.png --out QADIR       # 200% edge crops
imgqa detect-fringe OUTDIR/PHOTO.cutout.png --json    # any halos?

# 3. On detections: a generous bbox (>=3x feather) -> local repair
imgqa defringe OUTDIR/PHOTO.cutout.png --bbox X,Y,W,H --out OUTDIR
#    -> repeat detect-fringe on the result (expect 0)

# 4. Export of responsive variants (WebP/AVIF) — if needed, ffmpeg or the Pillow plugin
```

The `--out` gate: QADIR cannot live inside the cutout directory (source policy — imgqa refuses it itself).

## Human gate (mandatory)

Final acceptance of the edges — especially **hair matting** — is done by a human from the QA evidence (3 backgrounds plus edge crops, read with your eyes through Read). The skill never declares the edges "done": the detector catches the "dense halo" class, and the quality of fine strands is not assessed automatically. Show the user a render on a contrasting background plus 1-2 edge crops and ask for acceptance.

## Traps (verified by the spike)

- The machine's TLS proxy breaks weight downloads (both pip and pooch): pip is cured by a `pip.ini` with trusted-host (already in the venv), and the weights by `curl -L --ssl-no-revoke` into `D:\venvs\imgqa-ml\models`, where **the filename must be `<model>.onnx`** (pooch looks up by fname, not by release name; the md5 is verified).
- exit 2 ≠ pass. Empty or invalid output from the ML venv is a tool error (validated by the engine: schema/status/command/outputs plus the files inside --out).
- The QA verdict goes to `mb learn --task <TASK>`; defects on dogfood assets go into the regression catalogue (§9).
