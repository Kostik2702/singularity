---
name: design-analysis
description: Structural breakdown of a design mockup (PNG/JPG) into a design brief — sections, proportions, heights, palette, typography, compositional accents; the mockup's content is always UNVERIFIED. The numbers come from scripts/design-metrics.py and imgqa, with judgement layered on top of the numbers. Use when "break down the mockup", "write a design brief", design analysis, starting markup from a mockup, the input to design-fidelity-audit.
---

# design-analysis — mockup → a design brief with numbers

One of the two Stage 1 skills with a legitimate LLM judgement (aesthetics). The rule: **every judgement cites a number** from the tools; free-form "by eye" assessments are forbidden (contrast by eye has already been wrong: 4.2 against the real 4.93).

> **Zero-code rule:** dimensions, palette and section boundaries are computed by `design-metrics.py`, transparency and format by `imgqa probe`, pair contrast by `imgqa contrast`. New scripts: 0.

## The order

```bash
# 1. Technical context of the mockup (format, alpha, whether it is a baked checkerboard)
imgqa probe MOCKUP.png --json

# 2. Numbers: dimensions, aspect, dominant palette, section candidates
E:\Singularity\.venv\Scripts\python.exe \
  "E:\Singularity\singularity\skills\design-analysis\scripts\design-metrics.py" \
  MOCKUP.png --out "<evidence dir OUTSIDE the mockup tree>" --json

# 3. Multimodal Read of the mockup — judgement ON TOP of the step 2 numbers
# 4. Key text colour pairs — imgqa contrast (numbers go into the brief)
```

## Method of the breakdown (step 3)

- **Sections:** `data.bands` are boundary candidates from row colour changes. The judgement: merge or split candidates by meaning (a band is not automatically a section), and give each section its top/bottom/height/share_pct from bands.
- **Proportions:** the mockup's `aspect_h_over_w` is the anchor for fidelity (NIKA precedent: mockup 1.78×, implementation 5.09× — a category A defect, "vertically inflated").
- **Palette:** `dominant_colors` (hex + share) is the background and atmosphere; accent colours of small elements are picked multimodally with an eyedropper and CONFIRMED as a pair through `imgqa contrast`.
- **Typography:** the scale (display/heading/body/caption) is described RELATIVELY (as fractions of the section height), never in absolute px by eye.
- **Compositional accents:** overlaps, diagonals, the visual stage; note "what holds the section together" (NIKA hero lesson: the phone overlap is an art-directed stage, not flow).

## UNVERIFIED — a hard gate

All TEXT and data in the mockup (figures, prices, reviews, names, contacts) are art direction, NOT facts. In the brief every such element is marked `UNVERIFIED` and goes to the owner as a list of questions. The NIKA truth-gate lesson: "can the mockup be treated as factual content? — no". design-metrics.py deliberately does not extract text.

## Design-brief format (the output)

A note (in the vault or the evidence dir) with these sections: 1) Source + numbers (dims/aspect/palette — from metrics.json); 2) Sections (a table: name, top-bottom, height, share%, purpose); 3) Typography (the scale, relative); 4) Colour and contrast (pairs + ratio from imgqa); 5) Compositional accents; 6) UNVERIFIED content → questions for the owner; 7) Priorities for implementation.

The brief must be a usable input for [[design-fidelity-audit]]: its numbers are the reference for A/B categorisation.

## Gates

- [ ] `schema` = `design-metrics/1`; exit 2 means the tool did not run.
- [ ] Not a single dimension or colour in the brief without a reference to metrics.json or imgqa output.
- [ ] The UNVERIFIED list is always present, even when empty ("UNVERIFIED: none" as an explicit line).
- [ ] The owner accepts the brief (human gate) before markup starts.
