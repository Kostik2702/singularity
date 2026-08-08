"""The capture tool refuses to compare two different question sets.

Rows are paired by `id`, so a capture whose Q01 asked a different question
lines up perfectly against today's Q01 and yields a displacement number about
nothing. `tests/test_search_eval.py` already refuses this for the retrieval
eval set; the code-index tool had the same shape of risk and no guard, which
became reachable the day the ten questions were re-worded.
"""
from __future__ import annotations

import json
import pytest
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
TOOL = ENGINE / "tools" / "code_index_baseline.py"


def _run(src: Path, verb: str = "render") -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), verb, str(src)],
                          cwd=ENGINE, capture_output=True, text=True)


def _capture_like(tmp_path: Path, queries: dict[str, str]) -> Path:
    rows = [{"id": qid, "query": q, "kind": "decision", "top": [],
             "engine_ms": 1, "wall_ms": 2} for qid, q in queries.items()]
    src = tmp_path / "capture.json"
    src.write_text(json.dumps({"variants": {"baseline": rows}}), encoding="utf-8")
    return src


def _current_queries() -> dict[str, str]:
    sys.path.insert(0, str(ENGINE / "tools"))
    from code_index_baseline import QUERIES
    return {q["id"]: q["query"] for q in QUERIES}


def test_a_capture_from_the_current_question_set_is_read():
    """The control. Without it, a refusal that fires on everything would satisfy
    the test below while making the tool useless."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src = _capture_like(Path(td), _current_queries())
        proc = _run(src)
    assert proc.returncode == 0, proc.stderr


def test_a_capture_from_another_question_set_is_refused_loudly():
    import tempfile
    drifted = dict(_current_queries())
    first = sorted(drifted)[0]
    drifted[first] = "a question nobody asked"
    with tempfile.TemporaryDirectory() as td:
        src = _capture_like(Path(td), drifted)
        proc = _run(src)
    assert proc.returncode == 2, "a mismatched question set must not be rendered"
    assert first in proc.stderr and "different question set" in proc.stderr


def test_the_pre_corpus_record_is_refused_and_not_told_to_overwrite_itself():
    """`docs/code-index-baseline.json` holds the 2026-07-29 C00 run under the
    pre-translation questions — a state that cannot be reproduced, because the
    code corpus exists now. The tool must refuse to pair it by id, AND must not
    advise re-recording it: that advice would destroy the evidence."""
    proc = _run(ENGINE / "docs" / "code-index-baseline.json")
    assert proc.returncode == 2
    assert "measures nothing" in proc.stderr, "the refusal must say why"
    assert "leave this file alone" in proc.stderr, \
        "a dated record must be offered as an option, not only re-recording"


def test_the_fresh_capture_is_accepted():
    """The other half of the pair: a refusal that fired on everything would
    satisfy the test above and leave the tool unusable."""
    proc = _run(ENGINE / "docs" / "code-index-baseline-2026-08-08.json")
    assert proc.returncode == 0, proc.stderr


#: Every shape that is not the current question set. The first version of the
#: refusal compared only the OVERLAP of ids, so it caught the one case it had
#: just been written for — same ids, re-worded text — and waved through a
#: capture with entirely different ids (rendered an empty table, exit 0) and one
#: with no `variants` key (KeyError, exit 1, which in this repo means "defects
#: found" rather than "the tool did not run").
NOT_THE_CURRENT_SET = {
    "disjoint ids": {"variants": {"baseline": [
        {"id": f"Z{i:02d}", "query": "unrelated", "kind": "x",
         "top": [], "engine_ms": 1, "wall_ms": 1} for i in range(1, 11)]}},
    "empty baseline": {"variants": {"baseline": []}},
    "no variants key": {"taken_at": "x"},
    "variants is not a dict": {"variants": []},
    "baseline is not a list": {"variants": {"baseline": {}}},
    "a row with no id": {"variants": {"baseline": [{"query": "x"}]}},
}


@pytest.mark.parametrize("shape", sorted(NOT_THE_CURRENT_SET))
def test_anything_that_is_not_the_current_question_set_is_refused(shape, tmp_path):
    """exit 2 — "the tool did not run" — for every one of them, never a
    traceback and never a quietly empty table."""
    src = tmp_path / "capture.json"
    src.write_text(json.dumps(NOT_THE_CURRENT_SET[shape]), encoding="utf-8")
    proc = _run(src)
    assert proc.returncode == 2, f"{shape}: exit {proc.returncode}, stdout={proc.stdout!r}"
    assert "Traceback" not in proc.stderr, f"{shape}: crashed instead of refusing"
    assert proc.stderr.strip(), f"{shape}: refused without saying why"
