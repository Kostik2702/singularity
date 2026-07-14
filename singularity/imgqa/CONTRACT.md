# imgqa CLI Contract — schema_version 2

Status: FROZEN 2026-07-14 (WSF-01b; owner-ratified semantics per REQUEST CHANGES
review, see vault `decisions/WEBSITE-FACTORY-PLAN-WSF-00-09.md`). Any change to
this file bumps `schema_version` and requires owner sign-off. Consumers MUST
fail loudly on an unknown `schema_version`.

## schema 2 amendments (2026-07-14, Phase-4 review findings F1-F9)

1. ERROR paths keep the JSON contract: argparse failures and missing optional
   deps emit a schema envelope on stdout with exit 2 (`tool` versions are null
   for absent libraries, never a crash).
2. bbox is STRICT: negative origin, or any part outside the image, is exit 2 —
   callers clamp generous margins themselves (a cropped bbox silently
   mis-measures; an out-of-bounds defringe was a successful no-op).
3. In-process commands trade the wall-clock timeout for a RESOURCE GUARD:
   inputs above 120,000,000 pixels (or Pillow's decompression-bomb limit) are
   exit 2. Subprocess commands (`sheet`, `cutout`) keep the 300 s timeout.
4. `--out` additionally rejects: descendants of an input's directory, and
   anything under `IMGQA_PROTECTED_ROOTS` (os.pathsep-separated; calling
   skills set it to the audited project root, e.g. the NIKA tree).
5. `sheet` naming carries the source extension as qualifier
   (`clip.sheet.mp4.jpg`) so same-stem videos cannot collide; temp files are
   unique per run; a failed directory run deletes the partial set.
6. `cutout` VALIDATES the ML-venv payload (schema_version/status/command/
   outputs present, status ok, outputs non-empty, files exist inside --out) —
   an exit-0 arbitrary dict is a tool error, not success.
7. `contrast` gates on the RAW ratio (adds `ratio_raw` to data); `ratio` stays
   the 2-decimal display value. GT-C1 is unaffected (gate reads raw).

## 1. JSON envelope (every subcommand, `--json` is always available)

```json
{
  "schema_version": 1,
  "command": "<name>",
  "status": "ok | error",
  "tool": {
    "imgqa": "<imgqa __version__>",
    "pillow": "<PIL.__version__>",
    "numpy": "<numpy.__version__>",
    "scipy": "<scipy.__version__ | null>"
  },
  "inputs": { "...canonicalized input paths and parameters..." },
  "data": { "...per-subcommand payload (section 4)..." },
  "verdict": { "...evaluative commands only (section 2)..." },
  "outputs": ["...files written, absolute paths (writing commands only)..."],
  "error": "present iff status=error"
}
```

`tool` version evidence is MANDATORY in every run (protects GT comparisons from
silent library drift — REQUEST CHANGES item 3).

## 2. Exit semantics by contract class

| Class | exit 0 | exit 1 | exit 2 |
|---|---|---|---|
| evaluative (`probe`, `compare`, `contrast`, `detect-fringe`) | executed, verdict PASS | executed, verdict FAIL/defects found | tool error |
| informational (`edges`, `backgrounds`, `selftest`) | executed successfully | — never — | tool error |
| transform (`sheet`, `defringe`) | executed successfully | — never — | tool error |
| dispatch (`cutout`) | ML venv executed successfully | — never — | tool error (incl. ML venv missing/unreachable) |

Hard rules:
- Exit 2 must NEVER be mapped to "pass"/"no defects" by any consumer. A skill
  reading imgqa output must distinguish "clean" from "tool did not run".
- Evaluative verdicts are ALSO emitted in `verdict` (typed), exit code is a
  convenience mirror for shell gating. Verdicts are retained in the invoking
  task's development history (vault learnings) by the calling skill.

## 3. Filesystem policy (writing commands: `edges`, `backgrounds`, `sheet`, `defringe`, `cutout`)

- `--out DIR` is REQUIRED. It is created if absent, canonicalized
  (resolve + absolute) before use.
- Refused with exit 2: `--out` equal to or an ancestor of any input file's
  directory; any output path that would overwrite an input file. In-place
  modification is impossible by construction.
- Atomic writes: tmp file in the same directory + `os.replace`.
- Deterministic naming: `<input-stem>.<subcommand>[.<qualifier>].<ext>`
  (e.g. `hero.edges.boot-l.png`, `vid-01.sheet.jpg`). Re-running with the same
  arguments reproduces the same paths and contents (idempotent).
- Default timeout per subcommand: 60 s; `sheet` and `cutout`: 300 s. On
  timeout: exit 2.

## 4. Subcommand payloads (data)

- `probe <img>`: `{format, mode, size:[w,h], has_alpha, alpha_stats:{min,max,mean,pct_translucent}, baked_checkerboard:bool, checkerboard_evidence}`. Verdict: PASS = real usable alpha (has_alpha && !baked_checkerboard), FAIL otherwise.
- `edges <img> --out`: crops of alpha-boundary regions at 200% zoom, lossless PNG only. `{regions:[{bbox:[x,y,w,h], path}]}`.
- `backgrounds <img> --out`: composites on `#000`, `#FFC0CB`-class pink, `#FFF`. `{renders:[{background, path}]}`.
- `compare <a> <b>`: preprocessing FIXED as: decode → convert both to RGBA 8-bit → sizes must be equal (mismatch = verdict FAIL with reason `size-mismatch`, not resize). Metrics: `{mae, max_delta, diff_pixels, diff_ratio, threshold}` where `diff_pixels` counts pixels with any channel |Δ| > `--threshold` (default 2). Optional `--bbox` restricts the region. Verdict: PASS iff `mae <= --max-mae` AND `diff_ratio <= --max-diff-ratio` (both flags required when used as a gate).
- `contrast <fg> <bg>`: exact WCAG 2.x relative luminance; `{ratio, aa_normal, aa_large, aaa_normal}` with ratio rounded to 2 decimals. Verdict: PASS iff ratio >= `--min` (default 4.5).
- `detect-fringe <img>`: deterministic alpha-boundary scan; `{detections:[{bbox:[x,y,w,h], score, kind:"fringe|halo"}]}`. Verdict: PASS = no detections, FAIL = detections present. Must NOT flag fine hair matting (see GT-N below).
- `defringe <img> --bbox X,Y,W,H [--bbox ...] --out`: localized alpha remap + soft blend-back (algorithm reference: NIKA `cleanup_alpha_fringe.py`), parameters explicit, never global-threshold.
- `cutout <img> --out`: subprocess into the ML venv (path from `IMGQA_ML_VENV` env var or `~/.singularity.json` key `imgqa_ml_venv`; default `D:\venvs\imgqa-ml`). Typed JSON crosses the boundary; missing/broken venv = exit 2 with explicit remediation message.
- `selftest`: generates a synthetic RGBA image in a temp dir, runs probe/edges/compare on it, verifies envelope parse, exit codes, and atomic-write behavior. Designed to be run in BOTH PowerShell and Git Bash.

## 5. GT tolerances (OD-3; changes require owner sign-off)

| GT test | Gate |
|---|---|
| GT-P1 `detect-fringe` positive (NIKA seated-hero source, boot region) | ≥1 detection with ≥90% of its own area inside the reference REPAIR region (330,1620,680,428 — the generous fix margin from NIKA cleanup_alpha_fringe.py; the original "covers ≥60% of the region" wording was unachievable against a generous margin — amended 2026-07-14 during WSF-01c calibration, flagged for owner sign-off) |
| GT-N1 `detect-fringe` negative (hero-master.png clean result) | 0 detections |
| GT-N2 `detect-fringe` negative (hair-matting crop of the SAME source) | 0 detections |
| GT-D1 `defringe` reproduction (boot bbox, vs hero-master crop) | within bbox: `mae <= 1.0` AND `diff_ratio(threshold=2) <= 1%` |
| GT-C1 `contrast` | `C9586B` on `070506` → 4.93 ± 0.005 |
| GT-PR1 `probe` (SD3 source) | has_alpha=true, baked_checkerboard=false (categorical, exact) |
| GT-PR2 `probe` (synthetic 24bpp checkerboard fixture) | baked_checkerboard=true (categorical, exact) |

First calibration run may reveal a tolerance is wrong in either direction; the
number is then changed ONLY with an owner-approved edit of this file (+ schema
note), never silently in test code.

## 6. schema_version policy

Bump on: envelope field change, exit-semantics change, subcommand
addition/removal/reclassification, tolerance-table change. Consumers (skills,
agents) pin the versions they understand and fail loudly otherwise.
