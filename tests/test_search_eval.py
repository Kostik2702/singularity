"""Gate for the search evaluation set and its metrics.

Two things are checked here, and neither of them touches a vault:

1. **The fixture is well-formed and balanced.** `singularity/search/lexical.py`
   cites `tests/fixtures/search_eval.json` as the reason BM25's k1/b and RRF's
   k are not exposed as config. For a year that citation pointed at a file that
   did not exist -- the docstring read as "we measured this" while nothing had
   been measured. These tests exist so the citation cannot go hollow again: if
   the fixture is deleted, shrinks below its quota, or grows dangling targets,
   the suite goes red.

2. **The metric machinery is correct.** recall@k and MRR are computed here, not
   in the tool, so they can be proven on synthetic rankings where the right
   answer is known by construction. A recall number nobody can check is the
   same class of problem as a missing evaluation set.

The corpus-dependent measurement itself is NOT a test -- it needs the live
vault, which differs per machine. It lives in `tools/search_eval.py` with a
recorded snapshot under `docs/`, the same pattern as the code-index baseline.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.search_eval import (  # noqa: E402
    first_hit_rank,
    load_fixture,
    ranked_files,
    score_cases,
    verify_targets,
)

FIXTURE_PATH = REPO / "tests" / "fixtures" / "search_eval.json"


# --- the fixture cannot quietly disappear or drift -------------------------

def test_fixture_exists_where_lexical_says_it_does():
    """`lexical.py` names this exact path as its authority. If the name moves,
    the docstring must move with it -- a citation to a missing file is worse
    than no citation, because it reads as evidence."""
    assert FIXTURE_PATH.is_file(), (
        f"{FIXTURE_PATH} is missing, but singularity/search/lexical.py cites it "
        "as the reason BM25/RRF constants are fixed")
    cited = (REPO / "singularity" / "search" / "lexical.py").read_text(encoding="utf-8")
    assert "tests/fixtures/search_eval.json" in cited


def test_fixture_meets_its_own_quota():
    """Each class earns its place: identifiers are why BM25 exists, crosslingual
    is the multilingual-model claim, natural is the ordinary case. A set that
    loses a class stops being able to say WHERE a change helped or hurt."""
    data = load_fixture(FIXTURE_PATH)
    counts: dict[str, int] = {}
    for case in data["cases"]:
        counts[case["class"]] = counts.get(case["class"], 0) + 1
    assert counts == data["quota"], f"class counts {counts} != declared quota {data['quota']}"


def test_fixture_cases_are_distinct_and_well_formed():
    data = load_fixture(FIXTURE_PATH)
    cases = data["cases"]
    ids = [c["id"] for c in cases]
    queries = [c["query"] for c in cases]
    assert len(set(ids)) == len(ids), "duplicate case ids"
    assert len(set(queries)) == len(queries), "duplicate queries inflate a class silently"
    for case in cases:
        assert case["expect"], f"{case['id']} has no expected note"
        assert case.get("why"), f"{case['id']} has no rationale — an unexplained pair cannot be audited"
        for rel in case["expect"]:
            assert rel.endswith(".md"), f"{case['id']}: {rel} is not a note"
            assert not Path(rel).is_absolute(), f"{case['id']}: {rel} must be vault-relative"
            assert ".." not in Path(rel).parts, f"{case['id']}: {rel} escapes the vault"


# --- the metrics are provable on rankings whose answer is known ------------

def test_ranked_files_collapses_chunks_to_notes():
    """Three chunks of one note are one found note, not three. Counting chunks
    would rank a verbose note above a precise one."""
    results = [
        {"meta": {"file": "a.md"}}, {"meta": {"file": "a.md"}},
        {"meta": {"file": "b.md"}}, {"meta": {"file": "a.md"}},
    ]
    assert ranked_files(results) == ["a.md", "b.md"]


def test_ranked_files_ignores_malformed_entries():
    results = [{"meta": {}}, {"no_meta": 1}, {"meta": {"file": ""}}, {"meta": {"file": "c.md"}}]
    assert ranked_files(results) == ["c.md"]


@pytest.mark.parametrize("ranking,expect,want", [
    (["a.md", "b.md"], ["a.md"], 1),
    (["a.md", "b.md"], ["b.md"], 2),
    (["a.md", "b.md"], ["z.md"], None),
    (["a.md", "b.md"], ["b.md", "a.md"], 1),   # any accepted target counts
    ([], ["a.md"], None),
])
def test_first_hit_rank(ranking, expect, want):
    assert first_hit_rank(ranking, expect) == want


def test_score_cases_perfect_and_empty_rankings():
    perfect = [{"class": "identifier", "rank": 1} for _ in range(4)]
    m = score_cases(perfect)["_overall"]
    assert m["recall@1"] == 1.0 and m["mrr"] == 1.0 and m["misses"] == 0

    allmiss = [{"class": "identifier", "rank": None} for _ in range(4)]
    m = score_cases(allmiss)["_overall"]
    assert m["recall@1"] == m["recall@5"] == m["recall@10"] == 0.0
    assert m["mrr"] == 0.0 and m["misses"] == 4


def test_score_cases_cutoffs_are_cumulative():
    """A hit at rank 3 counts for @5 and @10 but not @1 — the property that
    makes a rise in R@1 mean 'moved to the top', not merely 'found at all'."""
    m = score_cases([{"class": "natural", "rank": 3}])["_overall"]
    assert m["recall@1"] == 0.0
    assert m["recall@5"] == 1.0
    assert m["recall@10"] == 1.0
    assert m["mrr"] == pytest.approx(1 / 3, abs=1e-4)


def test_score_cases_reports_each_class_separately():
    """The whole point of the class split: a change that helps identifiers and
    hurts crosslingual must not average out to 'no effect'."""
    rows = [
        {"class": "identifier", "rank": 1}, {"class": "identifier", "rank": 1},
        {"class": "crosslingual", "rank": None}, {"class": "crosslingual", "rank": None},
    ]
    m = score_cases(rows)
    assert m["identifier"]["recall@1"] == 1.0
    assert m["crosslingual"]["recall@1"] == 0.0
    assert m["_overall"]["recall@1"] == 0.5
    assert m["identifier"]["n"] == m["crosslingual"]["n"] == 2
    assert m["_overall"]["n"] == 4


def test_a_regression_moves_the_number_down():
    """The invariant this whole file protects, proven by planting the violation:
    degrade a perfect run to a miss and every metric that should fall, falls.
    A scorer that returned a constant would pass every test above and fail here.
    """
    good = score_cases([{"class": "natural", "rank": 1},
                        {"class": "natural", "rank": 1}])["_overall"]
    degraded = score_cases([{"class": "natural", "rank": 1},
                            {"class": "natural", "rank": None}])["_overall"]
    assert degraded["recall@1"] < good["recall@1"]
    assert degraded["recall@10"] < good["recall@10"]
    assert degraded["mrr"] < good["mrr"]
    assert degraded["misses"] > good["misses"]


# --- the recorded snapshot stays readable ----------------------------------

def test_recorded_baseline_is_parseable_and_covers_every_case():
    """`docs/search-eval-baseline.json` is the historical record a future
    ranking change is argued against. If it stops matching the fixture size,
    the comparison is between different sets and means nothing."""
    snapshot = REPO / "docs" / "search-eval-baseline.json"
    assert snapshot.is_file(), "baseline snapshot missing; re-record with tools/search_eval.py run --out"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    runs = data.get("runs", [data])
    n_fixture = len(load_fixture(FIXTURE_PATH)["cases"])
    for run in runs:
        assert len(run["cases"]) == n_fixture, (
            "baseline was recorded against a different fixture size; "
            "re-record rather than compare across sets")
        assert run["metrics"]["_overall"]["n"] == n_fixture


# --- the code corpus half of the set ---------------------------------------

def test_code_fixture_meets_its_quota_and_is_well_formed():
    """Same guarantees as the memory set, separately: code and memory are
    different corpora with different query shapes, and one set silently
    absorbing the other would hide which of them a change moved."""
    data = load_fixture(FIXTURE_PATH)
    cases = data["code_cases"]
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["class"]] = counts.get(case["class"], 0) + 1
    assert counts == data["code_quota"], f"{counts} != {data['code_quota']}"

    ids = [c["id"] for c in cases]
    assert len(set(ids)) == len(ids)
    for case in cases:
        assert case["expect"] and case.get("why")
        for rel in case["expect"]:
            assert not Path(rel).is_absolute()
            assert ".." not in Path(rel).parts


def test_a_target_whose_case_drifted_is_reported_missing(tmp_path):
    """Ranking compares paths byte for byte, so an expectation spelled
    `singularity/...` never matches a result `Singularity/...`. On a
    case-insensitive filesystem the file still "exists", and the set then
    scores every such case a miss while verification stays green -- exactly
    what happened after the repositories root started reaching this engine
    through a junction named `Singularity`: 12 of 16 code cases turned into
    misses and nothing failed. Verification must compare the ON-DISK spelling."""
    (tmp_path / "Repo" / "pkg").mkdir(parents=True)
    (tmp_path / "Repo" / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    cases = [
        {"id": "ok", "expect": ["Repo/pkg/mod.py"]},
        {"id": "drifted", "expect": ["repo/pkg/mod.py"]},
        {"id": "absent", "expect": ["Repo/pkg/gone.py"]},
    ]
    missing = verify_targets(cases, tmp_path)
    assert "ok -> Repo/pkg/mod.py" not in " ".join(missing)
    assert any(m.startswith("drifted") for m in missing)
    assert any(m.startswith("absent") for m in missing)


def test_the_two_sets_do_not_share_case_ids():
    """They are reported separately and stored together; colliding ids would
    make a per-case diff between two runs silently pair the wrong rows."""
    data = load_fixture(FIXTURE_PATH)
    memory_ids = {c["id"] for c in data["cases"]}
    code_ids = {c["id"] for c in data["code_cases"]}
    assert not (memory_ids & code_ids)
