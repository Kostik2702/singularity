"""The call-resolution yardstick scores what it claims to score (CODE-GRAPH P0).

Every check here is a planted violation: a wrong edge, a false edge on an
external call, a label whose call site moved or vanished, a naive resolver
that guesses by name. If the scorer or the locator went soft, a test below
turns red before a precision number built on them could mislead anyone.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tools.code_graph_yardstick import (  # noqa: E402
    FIXTURE,
    GATE,
    _py_defs,
    _sha256,
    _ts_defs,
    definitions_by_name,
    gate,
    load_fixture,
    locate,
    name_first,
    name_unique,
    score,
    validate_fixture,
)


def _edge(eid, expect, category="same-file", **kw):
    base = {"id": eid, "unit": "u", "file": "a.py", "line": 1, "call_text": "f()",
            "caller": "a.py::g", "callee_name": "f", "expect": expect,
            "category": category, "why": "test"}
    base.update(kw)
    return base


# --- scoring ------------------------------------------------------------------

def test_score_separates_correct_wrong_missed_and_false_edges():
    edges = [_edge("tp", "a.py::f"), _edge("wrong", "a.py::f"), _edge("miss", "a.py::f"),
             _edge("tn", None, "builtin-external"), _edge("fe", None, "external-collision")]
    preds = {"tp": "a.py::f", "wrong": "b.py::f", "miss": None, "tn": None, "fe": "a.py::f"}
    o = score(edges, preds)["_overall"]
    assert (o["tp"], o["wrong"], o["miss"], o["tn"], o["false_edge"]) == (1, 1, 1, 1, 1)
    assert o["precision"] == round(1 / 3, 4)      # 1 right of 3 emitted edges
    assert o["recall"] == round(1 / 3, 4)         # 1 found of 3 first-party sites
    assert o["false_edge_rate"] == 0.5            # 1 of 2 external sites got an edge


def test_an_unanswered_site_is_a_miss_or_a_correct_abstention():
    """A resolver that answers nothing must not look precise by default: it
    abstains correctly on external calls and misses every first-party one."""
    edges = [_edge("fp", "a.py::f"), _edge("ext", None, "builtin-external")]
    o = score(edges, {})["_overall"]
    assert o["miss"] == 1 and o["tn"] == 1
    assert o["precision"] is None and o["recall"] == 0.0


def test_a_perfect_resolver_scores_one_and_a_constant_one_does_not():
    edges = [_edge("a", "a.py::f"), _edge("b", "a.py::h"), _edge("c", None, "builtin-external")]
    perfect = score(edges, {"a": "a.py::f", "b": "a.py::h", "c": None})["_overall"]
    constant = score(edges, {k: "a.py::f" for k in "abc"})["_overall"]
    assert perfect["precision"] == perfect["recall"] == 1.0
    assert constant["precision"] < 1.0 and constant["false_edge_rate"] == 1.0


def test_the_gate_rejects_each_axis_on_its_own():
    """Each threshold must bite alone: a resolver that is precise but finds
    nothing, or finds everything but invents edges, fails the gate."""
    ok = {"precision": GATE["precision_min"], "recall": GATE["recall_min"],
          "false_edge_rate": GATE["false_edge_max"]}
    assert gate(ok) == []
    assert gate(dict(ok, precision=GATE["precision_min"] - 0.01))
    assert gate(dict(ok, recall=GATE["recall_min"] - 0.01))
    assert gate(dict(ok, false_edge_rate=GATE["false_edge_max"] + 0.01))
    assert gate(dict(ok, precision=None))          # emitted nothing: not a pass


def test_the_recorded_naive_baselines_miss_the_owners_gate():
    """P0 acceptance, pinned: the committed baseline shows both name-only
    resolvers failing the gate. If a re-record ever shows one passing, the set
    has gone soft and gates nothing."""
    import json
    runs = json.loads((REPO / "docs" / "code-graph-yardstick-baseline.json").read_text(encoding="utf-8"))
    for name in ("name-first", "name-unique"):
        assert gate(runs[name]["_overall"]), f"{name} passes the gate: the set cannot fail it"


# --- the fixture itself -------------------------------------------------------

def test_the_committed_fixture_is_well_formed():
    data = load_fixture(FIXTURE)
    assert validate_fixture(data) == []
    assert len(data["edges"]) >= 45


def test_validate_fixture_names_each_planted_defect():
    data = {"units": {"u": {}}, "files": {"u|a.py": "x"}, "edges": [
        _edge("dup", "a.py::f"), _edge("dup", "a.py::f"),
        {k: v for k, v in _edge("noexpect", "a.py::f").items() if k != "expect"},
        _edge("notqname", "f"),
        _edge("badcat", "a.py::f", category="nonsense"),
        _edge("badunit", "a.py::f", unit="other"),
    ]}
    joined = "\n".join(validate_fixture(data))
    for needle in ("duplicate edge ids", "noexpect: missing expect", "notqname: expect is not a qname",
                   "badcat: unknown category", "badunit: unit 'other' not declared"):
        assert needle in joined, needle


# --- relocation: a moved label is re-found, a vanished one fails --------------

def test_locate_keeps_intact_sites_follows_moved_ones_and_fails_vanished_ones(tmp_path):
    src = tmp_path / "a.py"
    src.write_text("def g():\n    f()\n    h()\n", encoding="utf-8")
    pinned = {"u|a.py": _sha256(src)}
    edges = [_edge("intact", "a.py::f", line=2),
             _edge("moved", "a.py::h", line=2, call_text="h()"),
             _edge("gone", "a.py::k", line=2, call_text="k()")]
    live, notes, failures = locate(edges, pinned, lambda unit: tmp_path)
    ids = {e["id"]: e for e in live}
    assert set(ids) == {"intact", "moved"}
    assert ids["moved"]["line"] == 3
    assert any("gone" in f for f in failures)

    src.write_text("def g():\n    f()\n    h()\n# edited\n", encoding="utf-8")
    _, notes, _ = locate([edges[0]], pinned, lambda unit: tmp_path)
    assert any("file changed, call site intact" in n for n in notes)


# --- the naive resolvers the set has to defeat --------------------------------

def test_python_definitions_carry_their_enclosing_class():
    lines = ["class Config:", "    def save(self):", "        def inner():", "            pass",
             "def save():", "    pass"]
    got = dict((q, n) for n, q in _py_defs("c.py", lines))
    assert "c.py::Config.save" in got and "c.py::Config.save.inner" in got
    assert "c.py::save" in got


def test_ts_definitions_see_classes_methods_and_arrow_functions():
    lines = ["export class JobService {", "  constructor(private repo: Repo) {}",
             "  async enqueue(job: Job): Promise<void> {", "    if (x) { return; }", "  }", "}",
             "export function retryWithBackoff(n: number) {", "}",
             "export const toKey = (s: string): string => s;"]
    qnames = {q for _, q in _ts_defs("s.ts", lines)}
    assert {"s.ts::JobService", "s.ts::JobService.enqueue", "s.ts::retryWithBackoff",
            "s.ts::toKey"} <= qnames
    assert "s.ts::JobService.if" not in qnames and "s.ts::JobService.constructor" not in qnames


def test_name_guessing_is_fooled_by_a_colliding_external_call(tmp_path):
    """The shape the council measured in the field: `counts.items()` on a dict
    resolved to a first-party `items`. A name-only resolver must produce that
    false edge here, or the set could never catch it."""
    (tmp_path / "inbox.py").write_text("def items(cfg):\n    return []\n", encoding="utf-8")
    (tmp_path / "use.py").write_text("def run(d):\n    return d.items()\n", encoding="utf-8")
    table = {"u": definitions_by_name(tmp_path)}
    edges = [_edge("coll", None, "external-collision", file="use.py", callee_name="items")]
    assert name_first(edges, table)["coll"] == "inbox.py::items"
    assert name_unique(edges, table)["coll"] == "inbox.py::items"
    assert score(edges, name_first(edges, table))["_overall"]["false_edge"] == 1
