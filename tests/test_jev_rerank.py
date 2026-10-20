"""Search rerank by Jev: the order changes only when Jev answers, and nothing
personal is ever sent."""
import json

import pytest
from jev_server import KEY, FakeJev, reply

from singularity import cli
from singularity.config import Config
from singularity.jev import log, rerank


def hit(title, file="notes/a.md", content="body of the note", source="memory", project=None):
    meta = {"file": file, "title": title, "section": "S", "content": content}
    if project:
        meta["project"] = project
    return {"meta": meta, "score": 0.5, "source": source}


def judge(by_title: dict):
    """A Jev that rates each candidate by its note title."""
    def body(req):
        out = {qid: {"type": "noul", "noul": by_title[q["instructions"]["note"]["title"]]}
               for qid, q in req["questions"].items()}
        return {"model": "jev-1.13.0", "answers": out,
                "usage": {"input_tokens": 500, "output_tokens": 5}}
    return body


@pytest.fixture
def env(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    servers = []

    class Env:
        pass

    e = Env()
    e.vault = vault
    e.cfg = Config(vault=vault, data={"jev_enabled": True})

    def serve(*script):
        srv = FakeJev(script)
        servers.append(srv)
        monkeypatch.setenv("MB_JEV_ENDPOINT", srv.url)
        return srv

    e.serve = serve
    monkeypatch.setenv("MB_JEV_API_KEY", KEY)
    yield e
    for srv in servers:
        srv.close()


def titles(resp):
    return [h["meta"]["title"] for h in resp["results"]]


def test_inactive_unless_enabled_searching_keyed_and_not_opted_out(env, monkeypatch):
    assert rerank.active(env.cfg)
    assert not rerank.active(env.cfg, disabled=True)
    assert not rerank.active(Config(vault=env.vault, data={}))
    assert not rerank.active(Config(vault=env.vault, data={"jev_enabled": True, "jev_search": False}))
    monkeypatch.delenv("MB_JEV_API_KEY")
    monkeypatch.setattr(rerank.client, "resolve_key", lambda: None)
    assert not rerank.active(env.cfg)


def test_candidates_wanted_is_three_times_top_between_15_and_30():
    assert [rerank.candidates_wanted(n) for n in (1, 5, 8, 20, 30, 40)] == [15, 15, 24, 30, 30, 40]


def test_reorders_by_noul_and_cuts_back_to_top(env):
    env.serve(reply(200, judge({"A": 0.1, "B": 0.9, "C": 0.5, "D": 0.7})))
    resp = {"results": [hit("A"), hit("B"), hit("C"), hit("D")]}
    out = rerank.apply(env.cfg, resp, "q", 3, excerpt=None, explain=True)
    assert titles(out) == ["B", "D", "C"]
    assert out["reranked"] is True
    assert out["results"][0]["jev"] == 0.9 and out["results"][0]["explain"]["jev"] == 0.9
    [row] = log.read(env.cfg)
    assert row["kind"] == "search" and row["head"] == "q" and row["size"] is None
    assert row["confidence"] == pytest.approx(0.9) and row["cost"] > 0


def test_ties_keep_the_semantic_order(env):
    env.serve(reply(200, judge({"A": 0.5, "B": 0.5, "C": 0.5})))
    out = rerank.apply(env.cfg, {"results": [hit("A"), hit("B"), hit("C")]}, "q", 3, excerpt=None)
    assert titles(out) == ["A", "B", "C"] and out["reranked"] is True


@pytest.mark.parametrize("script", [
    [reply(401, {"detail": "no"})],
    [reply(200, "{broken")],
    [reply(200, {"model": "m", "answers": {"c0": {"noul": "x"}}, "usage": {}})],
])
def test_any_failure_leaves_the_original_order(env, script):
    env.serve(*script)
    resp = {"results": [hit("A"), hit("B"), hit("C")]}
    out = rerank.apply(env.cfg, resp, "q", 2, excerpt=None)
    assert titles(out) == ["A", "B"] and out["reranked"] is False
    assert "jev" not in out["results"][0]
    [row] = log.read(env.cfg)
    assert row["error"] and row["cost"] == 0


def test_a_personal_note_is_never_sent_and_keeps_its_slot(env):
    srv = env.serve(reply(200, judge({"A": 0.1, "B": 0.9, "Roots": 0.0})))
    env.cfg.data["personal_roots"] = ["diary"]
    hits = [hit("A"), hit("Secret", content="my private thought"),
            hit("Roots", file="diary/day.md", content="diary text"), hit("B")]
    hits[1]["meta"]["project"] = "personal"
    out = rerank.apply(env.cfg, {"results": hits}, "q", 4, excerpt=None)
    sent = srv.sent_text()
    assert "Secret" not in sent and "my private thought" not in sent
    assert "diary text" not in sent and "Roots" not in sent
    assert titles(out) == ["B", "Secret", "Roots", "A"]


def test_only_personal_candidates_means_no_request_at_all(env):
    srv = env.serve(reply(200, judge({})))
    resp = {"results": [hit("S", project="personal")]}
    out = rerank.apply(env.cfg, resp, "q", 3, excerpt=None)
    assert not srv.requests and out["reranked"] is False


def test_code_and_source_hits_are_not_sent(env):
    srv = env.serve(reply(200, judge({"N": 0.8})))
    hits = [hit("fn", source="code"), hit("raw", source="sources"), hit("N")]
    out = rerank.apply(env.cfg, {"results": hits}, "q", 3, excerpt=None)
    assert "fn" not in srv.sent_text() and "raw" not in srv.sent_text()
    assert titles(out) == ["fn", "raw", "N"]


def test_the_excerpt_is_capped_and_redacted(env):
    srv = env.serve(reply(200, judge({"A": 0.5})))
    secret = "sk-ant-api03-" + "B" * 40
    content = "x" * 700 + secret
    rerank.apply(env.cfg, {"results": [hit("A", content=content)]}, "the query", 3, excerpt=None)
    q = srv.requests[0]["body"]
    assert q["state"] == {"query": "the query"}
    excerpt = q["questions"]["c0"]["instructions"]["note"]["excerpt"]
    assert len(excerpt) == 600 and secret not in srv.sent_text()
    assert q["questions"]["c0"]["criteria"]["true"].startswith("the excerpt answers")


def test_shown_content_is_cut_back_to_the_normal_excerpt(env):
    env.serve(reply(200, judge({"A": 0.5})))
    out = rerank.apply(env.cfg, {"results": [hit("A", content="y" * 400)]}, "q", 3, excerpt=150)
    assert out["results"][0]["meta"]["content"] == "y" * 150 + "..."


# --- through `mb search` -----------------------------------------------------------

@pytest.fixture
def search(env, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: env.cfg)
    seen = {}

    def fake_search(cfg, payload):
        seen["payload"] = payload
        results = [hit(t, content=f"{t} " + "z" * 300) for t in "ABCD"]
        if payload["format"] == "json":
            return {"results": results[:payload["top_k"]], "time_ms": 1}
        lines = [f"{r['meta']['file']} | {r['score']:.2f} | [reference] | "
                 f"{r['meta']['title']}/S | {r['meta']['content'][:150]}..." for r in results]
        return {"lines": lines[:payload["top_k"]], "time_ms": 1}

    monkeypatch.setattr("singularity.search.client.search", fake_search)
    return seen


def test_flag_off_the_search_is_byte_identical(env, search, capsys):
    env.cfg.data.pop("jev_enabled")
    assert cli.main(["search", "q", "--top", "2"]) == 0
    off = capsys.readouterr().out
    assert search["payload"]["top_k"] == 2 and search["payload"]["format"] == "compact"
    assert "excerpt_chars" not in search["payload"]
    assert off == (f"1. notes/a.md | 0.50 | [reference] | A/S | A {'z' * 148}...\n"
                   f"2. notes/a.md | 0.50 | [reference] | B/S | B {'z' * 148}...\n-- 1ms\n")


def test_no_jev_flag_keeps_the_search_unchanged(env, search, capsys):
    srv = env.serve(reply(200, judge({})))
    assert cli.main(["search", "q", "--top", "2", "--no-jev"]) == 0
    assert search["payload"]["top_k"] == 2 and not srv.requests


def test_search_reranks_and_renders_the_same_line_shape(env, search, capsys):
    env.serve(reply(200, judge({"A": 0.1, "B": 0.2, "C": 0.9, "D": 0.3})))
    assert cli.main(["search", "q", "--top", "2", "--explain"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert search["payload"]["top_k"] == 15 and search["payload"]["excerpt_chars"] == 600
    assert lines[0].startswith("1. notes/a.md | 0.50 | [memory] | C/S | C ")
    assert lines[0].endswith(f"{'z' * 148}... | jev=0.90")
    assert lines[1].startswith("2. ") and "| D/S |" in lines[1]


def test_search_json_carries_reranked_and_jev(env, search, capsys):
    env.serve(reply(200, judge({"A": 0.1, "B": 0.9, "C": 0.2, "D": 0.3})))
    assert cli.main(["search", "q", "--top", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reranked"] is True and [h["meta"]["title"] for h in out["results"]] == ["B", "D"]
    assert out["results"][0]["jev"] == 0.9


def test_search_survives_a_dead_jev(env, search, capsys):
    env.serve(reply(500, {"detail": "down"}))
    assert cli.main(["search", "q", "--top", "2", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["reranked"] is False and [h["meta"]["title"] for h in out["results"]] == ["A", "B"]


def test_a_top_beyond_30_reranks_the_first_30_and_keeps_the_rest(env):
    names = [f"N{i:02d}" for i in range(40)]
    env.serve(reply(200, judge({n: (1.0 if n == "N29" else 0.1) for n in names})))
    out = rerank.apply(env.cfg, {"results": [hit(n) for n in names]}, "q", 40, excerpt=None)
    assert titles(out)[0] == "N29" and titles(out)[30:] == names[30:]
    assert len(out["results"]) == 40


def test_a_note_under_a_personal_root_is_not_sent_even_with_a_project_line(env):
    srv = env.serve(reply(200, judge({"Open": 0.5})))
    env.cfg.data["personal_roots"] = ["diary"]
    hits = [hit("Hidden", file="diary/day.md", content="private words", project="work"),
            hit("Open")]
    out = rerank.apply(env.cfg, {"results": hits}, "q", 2, excerpt=None)
    assert "Hidden" not in srv.sent_text() and "private words" not in srv.sent_text()
    assert titles(out) == ["Hidden", "Open"]
    # the engine's own classification is untouched: that note is "work" there
    from singularity.search.chunking import infer_project
    assert infer_project({"project": "work"}, "diary/day.md", ["diary"]) == "work"


def test_whatever_ask_raises_the_original_results_come_back(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(rerank.client, "ask", boom)
    out = rerank.apply(env.cfg, {"results": [hit("A"), hit("B"), hit("C")]}, "q", 2, excerpt=None)
    assert titles(out) == ["A", "B"] and out["reranked"] is False
    assert log.read(env.cfg)[0]["error"] == "internal:null"


def test_a_garbled_http_answer_does_not_crash_the_search(env, monkeypatch):
    from jev_server import RawServer

    srv = RawServer(b"nonsense\r\n\r\n")
    try:
        monkeypatch.setenv("MB_JEV_ENDPOINT", srv.url)
        out = rerank.apply(env.cfg, {"results": [hit("A"), hit("B")]}, "q", 2, excerpt=None)
    finally:
        srv.close()
    assert titles(out) == ["A", "B"] and out["reranked"] is False
    assert log.read(env.cfg)[0]["error"] == "network:null"
