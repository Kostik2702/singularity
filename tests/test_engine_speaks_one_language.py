"""The engine is written in English, and its behaviour does not depend on yours.

Two different claims, and this file guards the first one mechanically.

The repository used to mix two languages in the same file — a docstring in one,
the sentence under it in the other, a marker list in a third place that quietly
made a feature work for speakers of one language only. Nothing failed when that
happened, so it kept happening. This test is the thing that fails.

What it looks for is a LETTER OUTSIDE THE LATIN BLOCKS, by code point, not a
list of words in any particular language: a guard against "Cyrillic" would have
said nothing about the day somebody pastes a Greek or Japanese heading into a
docstring. `MAX_LATIN` is the end of Latin Extended-B, so accented Latin
(`resume`, `naive`, `Muller`) passes and every other script does not.

Deliberate exceptions live in `ALLOWED` with a reason each. They are all one
shape: text in another language that is the DATA UNDER TEST, kept precisely
because the property being tested is that the engine does not care which
language it is handed. Deleting them would not clean anything up — it would
delete the evidence.

`NOT_YET_TRANSLATED` is different and is meant to shrink to nothing. It is the
part of the 2026-08-08 translation that is still outstanding, written down so it
stays visible: a file that silently keeps its old language is indistinguishable
from one nobody has looked at yet.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]

#: End of Latin Extended-B. Above it every letter belongs to another script.
MAX_LATIN = 0x24F

#: Two Greek letters that are not Greek here: they are mathematical notation
#: this codebase uses as operators — a sum that must reach 100%, and a delta
#: between two measurements. Flagging them would push four files onto an
#: allowlist for a reason that has nothing to do with language, and an
#: allowlist that fills up with false positives stops being read.
NOTATION = frozenset("ΣΔ")

#: Files whose non-Latin content is the point of the file, permanently.
ALLOWED: dict[str, str] = {
    "tests/fixtures/search_eval.json":
        "the retrieval eval set. Its `crosslingual` class exists to measure the "
        "one claim the multilingual model is here for, and its `expect` paths "
        "name real notes in the owner's vault - rewriting either would make the "
        "measurement a fiction",
    "tests/fixtures/grok/README.md":
        "the row documents a UTF-8 BOM leaking into the model context; the "
        "mis-decoded bytes ARE the observed artefact, not prose",
    "tests/test_routing.py":
        "one request written six ways, asserting the pushed text is identical "
        "for all of them - the invariant that the router stopped being a "
        "function of the user's keyboard",
    "tests/test_contradictions.py":
        "boolean and numeric flips in three scripts: the structural signals are "
        "the ones that CAN be universal, and this is what proves they are",
    "tests/test_gaps.py":
        "a heading that ends in a question mark is a question in any language; "
        "the detector tests that shape before it tests its English word list",
    "tests/test_chunking.py":
        "one section of the sample note is deliberately not Latin - an "
        "all-English fixture would pass with a Latin-only regex and stop "
        "testing anything",
    "tests/test_search_hybrid.py":
        "the BM25 tokenizer must not drop a script; a tokenizer that does makes "
        "those notes unfindable while the suite stays green",
    "tests/test_sources.py":
        "slugify over a non-ASCII title - the Windows-safe slug rules have to "
        "hold there too",
    "tests/test_context_map.py":
        "a non-ASCII fact behind a UTF-8 BOM: the two failure modes of the "
        "excerpt reader, together",
    "tests/test_rules_delivery.py":
        "the fixture writes one note under each spelling of the delivery-point "
        "heading, because the classifier has to recognise both and an "
        "English-only fixture would not notice if it stopped",
    "tests/test_scheduler.py":
        "a non-ASCII Windows path - the scheduler writes it into launchd/"
        "schtasks and the locale codec is where it breaks",
    "tests/test_gemini_crossplatform.py":
        "a non-ASCII Windows path plus a non-ASCII directory name; argv on "
        "Windows arrives through ANSI and this is the regression for it",
    "singularity/machine/rules_delivery.py":
        "one heading, quoted from the vault: 27 rule notes declare their "
        "delivery point under its Russian spelling, and the inventory check "
        "has to match the heading that is actually there or it reports 27 "
        "accounted notes as orphans and gets switched off for crying wolf. "
        "The spelling itself lives in `rules_delivery.DELIVERY_SECTION_MARKERS` "
        "and is deliberately NOT repeated here: a scanner may not write what it "
        "searches for. Vault content, not engine prose",
    "golden-registry/files/letter-glitch-wall/reference-implementation-index.html":
        "`const GLYPHS` is the alphabet the glitch wall renders. Those "
        "characters are the effect's pixels, not words addressed to a reader",
}

#: Recorded machine output over a vault that is not written in English. These
#: are evidence of what the system RETURNED on a given day; translating them
#: would fabricate results no run ever produced. They are data, like a log.
ALLOWED.update({
    "docs/search-eval-baseline.json": "recorded retrieval results, not prose",
    "docs/search-eval-code-baseline.json": "recorded retrieval results, not prose",
    "docs/v0.5.0-baseline.json": "recorded retrieval results, not prose",
    "docs/code-index-baseline.json": "recorded retrieval results, not prose",
    "docs/code-index-baseline-2026-08-08.json":
        "the re-record taken after the questions were re-worded. Same category "
        "as the others: what the vault returned, note paths and section names "
        "included",
    "docs/code-index-baseline.md":
        "its prose is English; the Q01-Q10 blocks are the queries as issued and "
        "the rows as returned, naming real notes in the vault. Rewriting them "
        "would turn a measurement into a fiction, and a re-record would put them "
        "straight back",
})

#: Outstanding work, not exceptions. Each line is a file the translation has not
#: reached yet. Removing a line is how the task finishes; ADDING one is a
#: regression and `test_the_backlog_only_shrinks` is what says so.
#: Empty since 2026-08-08: every file on it was translated. The name and the
#: guard below stay, because the next file that arrives in another language
#: needs somewhere honest to be recorded rather than quietly allowlisted.
NOT_YET_TRANSLATED: frozenset[str] = frozenset()

#: The backlog is empty, so the ceiling is zero. Left at 15 it would have let
#: fifteen files be quietly parked here instead of translated — a ratchet that
#: only bites once is a budget.
BACKLOG_CEILING = 0


def _tracked_text_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], cwd=ENGINE,
                         capture_output=True, text=True, check=True).stdout
    return [line for line in out.split("\n") if line]


def _foreign_letters(text: str) -> set[str]:
    """Letters above the Latin blocks. A set, so a report names the scripts
    rather than repeating one character three hundred times."""
    return {ch for ch in text
            if ord(ch) > MAX_LATIN and ch.isalpha() and ch not in NOTATION}


def _offenders() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel in _tracked_text_files():
        if rel in ALLOWED or rel in NOT_YET_TRANSLATED:
            continue
        path = ENGINE / rel
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue                      # binary or unreadable: not prose
        letters = _foreign_letters(text)
        if letters:
            found[rel] = letters
    return found


def test_no_file_outside_the_declared_list_is_written_in_another_language():
    """The gate itself.

    A file appearing here is not necessarily wrong — it may belong in `ALLOWED`.
    What it may not do is arrive without anyone deciding which."""
    offenders = _offenders()
    assert offenders == {}, (
        "non-Latin text outside the declared lists:\n  "
        + "\n  ".join(f"{rel}: {''.join(sorted(chars))[:40]}"
                      for rel, chars in sorted(offenders.items()))
        + "\n\nEither translate the file, or add it to ALLOWED with the reason "
          "its other language is the data under test.")


def test_every_exception_states_why():
    """An allowlist without reasons becomes a place to hide things. The reason
    is what a future reader checks against the file to decide whether the
    exception still holds."""
    for rel, why in ALLOWED.items():
        assert len(why) > 30, f"{rel}: an exception needs a stated reason, not a label"


def test_no_exception_is_stale():
    """An entry for a file that no longer contains anything foreign is an
    exception nobody re-examined. It also silently widens the gate: the day that
    path is reused for something else, the guard is already off for it."""
    tracked = set(_tracked_text_files())
    stale = []
    for rel in ALLOWED:
        assert rel in tracked, f"{rel}: allowlisted but not tracked by git"
        text = (ENGINE / rel).read_text(encoding="utf-8")
        if not _foreign_letters(text):
            stale.append(rel)
    assert stale == [], f"allowlisted but now clean — drop the entry: {stale}"


def test_the_backlog_only_shrinks():
    """`NOT_YET_TRANSLATED` is a debt, and a debt that can grow is a budget.
    Finishing a file means deleting its line here; nothing may add one."""
    assert len(NOT_YET_TRANSLATED) <= BACKLOG_CEILING, (
        "the translation backlog grew. A new file in another language belongs "
        "in ALLOWED with a reason, or translated — not on the backlog")
    tracked = set(_tracked_text_files())
    for rel in NOT_YET_TRANSLATED:
        assert rel in tracked, f"{rel}: on the backlog but not tracked by git"


@pytest.mark.parametrize("sample,expected", [
    ("plain ascii", False),
    ("naive resume Muller", False),        # Latin-1/Extended: still Latin
    ("1234 :=/*-", False),                 # digits and punctuation are not letters
])
def test_the_detector_passes_latin_and_symbols(sample, expected):
    assert bool(_foreign_letters(sample)) is expected


def test_the_detector_actually_catches_a_foreign_letter():
    """The control the absence checks above are owed: every assertion in this
    file is about NOT finding something, and a detector that finds nothing at
    all would satisfy all of them ([[Rule - a claim of absence requires a
    control]]). The specimen is built from code points so this file stays
    inside its own gate."""
    for code_point in (0x0410, 0x03A9, 0x3042, 0x4E2D):   # four different scripts
        specimen = "ok " + chr(code_point) + " ok"
        assert _foreign_letters(specimen) == {chr(code_point)}, \
            f"the detector missed U+{code_point:04X}"


#: How much non-Latin text each exception carried when it was granted. The
#: allowlist is FILE-granular, which is the hole this closes: `tests/test_sources.py`
#: is on it because of one slugify fixture, and until this budget existed a
#: whole paragraph of untranslated prose could be appended to that file and the
#: gate stayed green — precisely where nobody would look, because non-Latin text
#: there is expected.
#:
#: Growing an entry is legal and takes one edit; doing it without noticing is
#: what this forbids.
BUDGET: dict[str, int] = {
    "docs/code-index-baseline.json": 1011,
    "docs/code-index-baseline-2026-08-08.json": 1145,
    "docs/code-index-baseline.md": 700,
    "docs/search-eval-baseline.json": 12738,
    "docs/search-eval-code-baseline.json": 424,
    "docs/v0.5.0-baseline.json": 1908,
    "golden-registry/files/letter-glitch-wall/reference-implementation-index.html": 10,
    "singularity/machine/rules_delivery.py": 11,
    "tests/fixtures/grok/README.md": 2,
    "tests/fixtures/search_eval.json": 3347,
    "tests/test_chunking.py": 203,
    "tests/test_context_map.py": 29,
    "tests/test_contradictions.py": 87,
    "tests/test_gaps.py": 26,
    "tests/test_gemini_crossplatform.py": 24,
    "tests/test_routing.py": 83,
    "tests/test_rules_delivery.py": 11,
    "tests/test_scheduler.py": 27,
    "tests/test_search_hybrid.py": 67,
    "tests/test_sources.py": 28,
}


def _foreign_count(rel: str) -> int:
    text = (ENGINE / rel).read_text(encoding="utf-8")
    return sum(1 for ch in text
               if ord(ch) > MAX_LATIN and ch.isalpha() and ch not in NOTATION)


def test_every_exception_has_a_budget():
    """An entry with no budget is an exception with no ceiling."""
    assert set(BUDGET) == set(ALLOWED), (
        f"budget and allowlist disagree: "
        f"only in budget {sorted(set(BUDGET) - set(ALLOWED))}, "
        f"only in allowlist {sorted(set(ALLOWED) - set(BUDGET))}")


@pytest.mark.parametrize("rel", sorted(BUDGET))
def test_an_exception_does_not_grow_unnoticed(rel):
    """The gate is file-granular, so an allowlisted file is a place new
    untranslated text can accumulate invisibly. The count may fall freely; it
    may not rise without someone raising the number here in the same change."""
    assert _foreign_count(rel) <= BUDGET[rel], (
        f"{rel}: non-Latin text grew past its budget "
        f"({_foreign_count(rel)} > {BUDGET[rel]}). If the new text really is "
        f"data under test, raise the budget in the same change and say why.")
