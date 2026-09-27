"""`mb search --graph-boost` (CODE-GRAPH P7.3): a bounded re-rank of code hits
by the distinct callers the code graph knows for their symbol.

What is pinned, each seen red on a plant: the move is bounded (at most 3
places, whatever the signals); zero signal is the engine's own order; the
answer holds exactly the engine's hits; a file outside every graph or changed
since its graph was built is NEUTRAL, not "0 callers"; and the flag is off
unless given.
"""
from __future__ import annotations

import json
import random

import pytest

from singularity import cli
from singularity.codegraph import build, scope
from singularity.config import Config
from singularity.search import client
from singularity.search import graph_boost as gb

CORE = ("def helper(x):\n    return x + 1\n\n\n"
        "def lonely():\n    return 'nobody calls me'\n\n\n"
        "def one():\n    return helper(1)\n\n\n"
        "def two():\n    return helper(2) + helper(3)\n\n\n"
        "def three():\n    return helper(4)\n\n\n"
        "LIMIT = 10\n")


@pytest.fixture
def graphed(tmp_path):
    """One repository with a built graph, one without."""
    root = tmp_path / "Project"
    (root / "svc" / "app").mkdir(parents=True)
    (root / "svc" / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "svc" / "app" / "core.py").write_text(CORE, encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault",
                 data={"code_repos_dir": str(root), "code_graph_enabled": True})
    (svc,) = scope.discover_units(cfg)
    build.build_unit(cfg, svc)
    (root / "nograph").mkdir()
    (root / "nograph" / "x.py").write_text("def helper():\n    return 0\n", encoding="utf-8")
    return cfg, root


def _hit(file, first, last, source="code", **extra):
    return {"meta": {"file": file, "line_start": first, "line_end": last,
                     "chunk_id": f"{file}#{first}", **extra},
            "score": 0.5, "source": source}


# --- the formula -------------------------------------------------------------

def test_strength_is_zero_for_unknown_and_saturates():
    assert gb.strength(None) == gb.strength(0) == 0.0
    assert 0 < gb.strength(1) < gb.strength(4) < gb.strength(gb.SATURATION) == 1.0
    assert gb.strength(10_000) == 1.0


def test_zero_signal_is_the_engines_own_order():
    assert gb.boosted_order([0.0] * 12) == list(range(12))


def test_no_hit_ever_moves_up_more_than_three_places():
    """The bound the docstring claims, checked over random signal vectors
    rather than one hand-picked case."""
    rng = random.Random(7)
    for _ in range(2000):
        n = rng.randint(2, 20)
        strengths = [rng.choice([0.0, 0.0, rng.random(), 1.0]) for _ in range(n)]
        for new_pos, old_pos in enumerate(gb.boosted_order(strengths)):
            assert old_pos - new_pos <= 3, (strengths, old_pos, new_pos)


def test_a_boosted_hit_passes_a_near_neighbour():
    order = gb.boosted_order([0.0, 0.0, 1.0])
    assert order.index(2) < 2


# --- the join with the graph -------------------------------------------------

def test_callers_are_distinct_callers_of_the_innermost_symbol(graphed):
    cfg, _ = graphed
    graphs = gb.GraphIndex(cfg)
    try:
        # a v1 chunk knows only its lines
        assert graphs.signal(_hit("svc/app/core.py", 1, 2)["meta"])[:2] == (3, gb.OK)
        assert graphs.signal(_hit("svc/app/core.py", 5, 6)["meta"])[:2] == (0, gb.OK)
        # a v2 chunk names its symbol; its path is corpus-relative, the graph's unit-relative
        meta = _hit("svc/app/core.py", 1, 2, qname="svc/app/core.py::helper")["meta"]
        assert graphs.signal(meta) == (3, gb.OK, "app/core.py::helper")
        assert graphs.signal(_hit("svc/app/core.py", 25, 25)["meta"])[1] == gb.MODULE
    finally:
        graphs.close()


def test_code_without_a_graph_is_outside_not_zero_callers(graphed):
    cfg, _ = graphed
    graphs = gb.GraphIndex(cfg)
    try:
        assert graphs.signal(_hit("nograph/x.py", 1, 2)["meta"]) == (None, gb.OUTSIDE, None)
    finally:
        graphs.close()


def test_a_file_changed_since_its_graph_is_stale_not_trusted(graphed):
    cfg, root = graphed
    core = root / "svc" / "app" / "core.py"
    core.write_text("# a line on top moves every span\n" + CORE, encoding="utf-8")
    graphs = gb.GraphIndex(cfg)
    try:
        assert graphs.signal(_hit("svc/app/core.py", 2, 3)["meta"])[1] == gb.STALE
    finally:
        graphs.close()


def test_apply_reorders_within_the_same_hits_and_says_what_it_knew(graphed):
    cfg, _ = graphed
    results = [_hit("svc/app/core.py", 5, 6),                     # lonely: 0 callers
               _hit("howto/n.md", 1, 3, source="memory"),
               _hit("nograph/x.py", 1, 2),                        # no graph
               _hit("svc/app/core.py", 1, 2)]                     # helper: 3 callers
    resp = gb.apply(cfg, {"results": [dict(r) for r in results]}, explain=True)
    ids = [r["meta"]["chunk_id"] for r in resp["results"]]
    assert sorted(ids) == sorted(r["meta"]["chunk_id"] for r in results)   # nothing added or lost
    assert ids.index("svc/app/core.py#1") < 3                              # helper moved up
    assert resp["graph_boost"]["code_hits"] == 3 and resp["graph_boost"][gb.OUTSIDE] == 1
    assert gb.OUTSIDE in resp["graph_boost_warning"]
    helper = next(r for r in resp["results"] if r["meta"]["chunk_id"] == "svc/app/core.py#1")
    assert helper["explain"]["graph"]["callers"] == 3


def test_apply_without_any_graph_changes_nothing(tmp_path):
    cfg = Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path)})
    results = [_hit("a/x.py", 1, 2), _hit("b/y.py", 1, 2)]
    resp = gb.apply(cfg, {"results": list(results)})
    assert resp["results"] == results and resp["graph_boost"]["moved"] == 0


def test_apply_can_render_the_compact_lines(graphed):
    cfg, _ = graphed
    resp = gb.apply(cfg, {"results": [_hit("svc/app/core.py", 5, 6, kind="function",
                                            name="lonely", content="def lonely():")]},
                    as_lines=True)
    assert "results" not in resp
    assert resp["lines"][0].startswith("svc/app/core.py | 0.50 | [function] | lonely |")


# --- the flag ----------------------------------------------------------------

def _run(argv, cfg, monkeypatch, answer=None):
    seen = {}

    def search(cfg_, payload):
        seen.update(payload)
        return json.loads(json.dumps(answer or {"results": []}))

    monkeypatch.setattr(client, "search", search)
    args = cli._build_parser().parse_args(argv)
    return cli.cmd_search(cfg, args), seen


def test_the_boost_is_off_by_default(graphed, monkeypatch):
    cfg, _ = graphed
    code, payload = _run(["search", "helper", "--with-code"], cfg, monkeypatch, {"lines": []})
    assert code == 0 and payload["format"] == "compact"


def test_the_flag_needs_code_in_the_answer_and_the_graph_on(graphed, monkeypatch, capsys):
    cfg, _ = graphed
    assert _run(["search", "helper", "--graph-boost"], cfg, monkeypatch)[0] == 2
    assert "--with-code" in capsys.readouterr().err
    cfg.data["code_graph_enabled"] = False
    assert _run(["search", "helper", "--with-code", "--graph-boost"], cfg, monkeypatch)[0] == 2
    assert "code_graph_enabled" in capsys.readouterr().err


def test_the_flag_asks_for_hits_and_prints_them_boosted(graphed, monkeypatch, capsys):
    cfg, _ = graphed
    answer = {"results": [_hit("svc/app/core.py", 5, 6, kind="function", name="lonely"),
                          _hit("svc/app/core.py", 1, 2, kind="function", name="helper")]}
    code, payload = _run(["search", "helper", "--code-only", "--graph-boost"], cfg, monkeypatch,
                         answer)
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and payload["format"] == "json"
    assert out[0].startswith("1. svc/app/core.py | 0.50 | [function] | helper")
