"""The chunker gate and the boost comparison (CODE-GRAPH P7), without a vault.

The measurement itself needs the machine's repositories and model, so it is
a tool run recorded under docs/ (search-eval-code-v2.json). What CAN be proven
here is that the verdict follows the rule declared before the run -- "not
worse" means no metric loses more than one case's worth -- and that the
isolated copy can never be placed where it would write into live state.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools import code_index_baseline as cib  # noqa: E402
from tools import search_eval as se  # noqa: E402


def _run(metrics: dict[str, float], n: int = 20) -> dict:
    return {"metrics": {"_overall": {"n": n, **metrics}}}


BASE = {"recall@1": 0.35, "recall@5": 0.55, "recall@10": 0.55, "mrr": 0.4417}


def _pair(**v2_changes: float) -> tuple[dict, dict]:
    v1 = {"fusion_on": _run(BASE), "fusion_off": _run(BASE)}
    v2 = {"fusion_on": _run({**BASE, **v2_changes}), "fusion_off": _run(BASE)}
    return v1, v2


def test_equal_numbers_are_not_worse():
    assert se.gate_verdict(*_pair())["not_worse"]


def test_losing_exactly_one_case_is_still_not_worse():
    assert se.gate_verdict(*_pair(**{"recall@1": 0.30, "mrr": 0.3917}))["not_worse"]


@pytest.mark.parametrize("metric,value", [("recall@1", 0.25), ("recall@5", 0.45),
                                          ("recall@10", 0.4999), ("mrr", 0.39)])
def test_losing_more_than_one_case_on_any_metric_is_worse(metric, value):
    verdict = se.gate_verdict(*_pair(**{metric: value}))
    assert not verdict["not_worse"]
    assert [c["metric"] for c in verdict["checks"] if not c["pass"]] == [metric]


def test_both_fusion_modes_are_gated():
    v1 = {"fusion_on": _run(BASE), "fusion_off": _run(BASE)}
    v2 = {"fusion_on": _run(BASE), "fusion_off": _run({**BASE, "recall@5": 0.40})}
    verdict = se.gate_verdict(v1, v2)
    assert not verdict["not_worse"]
    assert {c["mode"] for c in verdict["checks"]} == {"fusion_on", "fusion_off"}


def test_the_copy_may_not_live_inside_the_vault_or_the_repositories(tmp_path):
    vault, repos = tmp_path / "vault", tmp_path / "repos"
    vault.mkdir()
    repos.mkdir()
    for inside in (vault / "scratch", repos / "x" / "scratch"):
        with pytest.raises(SystemExit, match="refusing scratch"):
            se._refuse_inside(inside, vault, repos)
    se._refuse_inside(tmp_path / "elsewhere", vault, repos)


def test_the_copy_skips_worktrees_and_never_graph_only_roots(tmp_path):
    class Real:
        hf_cache = tmp_path / "hf"

        @staticmethod
        def get(key):
            return {"model": "m", "code_skip_dirs": ["node-v20"],
                    "code_graph_extra_roots": ["E:/work/mono"]}.get(key)

    data = se._isolated_data(Real(), tmp_path, code_chunk_schema="v2")
    assert data["code_skip_dirs"] == ["node-v20", *se.SCRATCH_EXTRA_SKIP]
    assert data["code_graph_extra_roots"] == []
    assert data["code_repos_dir"] == str(tmp_path / "repos")
    assert data["code_chunk_schema"] == "v2" and data["model"] == "m"


# --- compare-boost -----------------------------------------------------------

def _row(qid: str, query: str, hits: list[tuple[str, str]]) -> dict:
    return {"id": qid, "query": query, "kind": "decision",
            "top": [{"chunk_id": c, "source": s} for c, s in hits]}


def _capture(tmp_path: Path, boosted_hits: list[tuple[str, str]]) -> Path:
    base_hits = [("n1", "memory"), ("c1", "code"), ("c2", "code")]
    rows = {v: [_row(q["id"], q["query"], base_hits) for q in cib.QUERIES]
            for v in ("baseline", "with_code")}
    rows["with_code_boost"] = [_row(q["id"], q["query"], boosted_hits) for q in cib.QUERIES]
    src = tmp_path / "capture.json"
    src.write_text(json.dumps({"variants": rows}), encoding="utf-8")
    return src


def _compare(src: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(REPO / "tools" / "code_index_baseline.py"),
                           "compare-boost", str(src)], cwd=REPO, capture_output=True, text=True)


def test_compare_boost_reports_where_things_moved(tmp_path):
    src = _capture(tmp_path, [("c1", "code"), ("n1", "memory"), ("c2", "code")])
    proc = _compare(src)
    assert proc.returncode == 0, proc.stderr
    row = cib.boost_rows(*[json.loads(src.read_text(encoding="utf-8"))["variants"][v]
                           for v in ("with_code", "with_code_boost")])[0]
    assert row["moved"] == 2 and row["top1_changed"]
    assert row["code_mean_rank"] == (2.5, 2.0) and row["note_mean_rank"] == (1.0, 2.0)


def test_compare_boost_fails_when_the_hit_set_changed(tmp_path):
    proc = _compare(_capture(tmp_path, [("c1", "code"), ("n1", "memory"), ("c9", "code")]))
    assert proc.returncode == 1 and "hit SET" in proc.stderr


def test_compare_boost_needs_a_boosted_capture(tmp_path):
    rows = [_row(q["id"], q["query"], []) for q in cib.QUERIES]
    src = tmp_path / "plain.json"
    src.write_text(json.dumps({"variants": {"baseline": rows, "with_code": rows}}),
                   encoding="utf-8")
    assert _compare(src).returncode == 2
