"""Every guarded rule proves it can still fail — on the ordinary suite run.

§2 has said "plant the violation" since the beginning, and the rule held right
up to the moment it cost something: eight verifiers written under a deadline
went out with no plants, and an independent review then planted 51 defects of
which 12 survived. `tools/plant.py` made the cycle cheap. It did not make it
happen — that still needed someone to remember, at the exact moment remembering
is least likely.

So the plants moved here. Nothing to remember: a vacuous gate now fails
`pytest` like any other defect. `tests/plants.json` is the declaration; adding a
guarded rule means adding a line to it.

Two properties this file depends on:

- **Controls, in the same run.** Cases expecting GREEN are not padding. Without
  them a bug that made every mutation look RED would leave the whole file
  passing while measuring nothing — "not found" is not evidence on its own
  ([[Rule - a claim of absence requires a control]]).
- **Serial execution.** A case mutates a real repo file for the length of one
  subprocess run. Under `pytest -n` another worker could observe the mutated
  tree, so this file must not be run in parallel with the suite it mutates.
"""
import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "tools"))
import plant  # noqa: E402

CASES = json.loads((ENGINE / "tests" / "plants.json").read_text(encoding="utf-8"))


def _ids():
    return [c["label"] for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=_ids())
def test_the_guard_still_catches_its_violation(case):
    """RED means the gate fired. GREEN on an `expect: RED` case means the gate
    has gone vacuous — it is still green on healthy code, which is precisely
    why nothing else would notice. ERROR means the run proved nothing at all
    and must never be read as either."""
    verdict, detail = plant.run_case(case)
    assert verdict == case["expect"], (
        f"{case['label']}: got {verdict}, expected {case['expect']} — {detail}")


def test_the_declaration_has_controls():
    """A file of only-RED cases cannot distinguish a working harness from one
    that reports RED unconditionally."""
    greens = [c for c in CASES if c["expect"] == "GREEN"]
    assert len(greens) >= 2, "at least two GREEN controls are required"


def test_no_case_names_one_machine_s_interpreter():
    """`.venv/bin/python` in every case turned all fifteen into ERROR on
    Windows (a venv keeps it in `Scripts`) and on every CI runner (no venv at
    all): the file measured nothing anywhere but the machine that wrote it.
    `{python}` is the interpreter running the suite. Planted: one case back on
    `.venv/bin/python` -- red."""
    for case in CASES:
        interpreters = [w for w in case["cmd"] if Path(w).name.lower().startswith("python")]
        assert interpreters == [], (
            f"{case['label']}: names {interpreters}; use {plant.PYTHON!r}, the interpreter running the suite")


def test_every_case_names_itself():
    for case in CASES:
        assert case.get("label"), f"unlabelled case on {case['file']}"
        assert case["old"] != case["new"], f"{case['label']}: mutation is a no-op"
