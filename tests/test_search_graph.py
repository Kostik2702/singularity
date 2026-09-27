"""`mb search --with-code --graph` (CODE-GRAPH P7, decision D1 row "Search").

Two promises, each proven by a planted violation:
* without `--graph` the search prints the SAME BYTES as before the flag
  existed (D2 stays in force): the golden under tests/fixtures/ was written by
  the code at dec1d60, before this change, from real engine responses on the
  world below;
* with it, every code hit says what the graph knows about it -- and a hit the
  graph cannot speak for says so ("outside graph universe", stale), never
  "0 callers".
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import singularity.search.indexer as indexer_mod  # noqa: E402
from singularity import cli  # noqa: E402
from singularity.codegraph import build, scope, store  # noqa: E402
from singularity.config import Config  # noqa: E402
from singularity.search import client  # noqa: E402
from singularity.search.engine import SearchEngine  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "search_graph_golden.json"
VOCAB = ["retry", "backoff", "invoice", "worker", "helper", "note", "settings"]

SVC = {
    ".gitignore": "gen/\n",
    "app/__init__.py": "",
    "app/core.py": ("def helper():\n"
                    '    """Retry helper with backoff."""\n'
                    "    return 1\n"
                    "\n"
                    "\n"
                    "class Worker:\n"
                    '    """Runs the retry worker."""\n'
                    "\n"
                    "    @staticmethod\n"
                    "    def run(x):\n"
                    '        """Run one retry of the worker."""\n'
                    "        return helper()\n"
                    "\n"
                    "\n"
                    "def main():\n"
                    '    """Retry entry point of the worker."""\n'
                    "    Worker.run(1)\n"
                    "    return helper()\n"),
    "app/other.py": ("from .core import helper\n"
                     "\n"
                     "\n"
                     "def use():\n"
                     '    """Invoice with the retry helper."""\n'
                     "    return helper()\n"
                     "\n"
                     "\n"
                     "def guess(x):\n"
                     '    """Retry whatever x is, with backoff."""\n'
                     "    return x.run()\n"),
    # no definition at all: the chunker windows it, the graph holds the file with no symbol
    "app/settings.py": "RETRY_BACKOFF_SECONDS = 3\nRETRY_ATTEMPTS_BEFORE_GIVING_UP = 5\n",
    # gitignored: in the code index (a filesystem scan), not in the graph (git's universe)
    "gen/built.py": 'def generated_retry():\n    """Generated retry backoff stub."""\n    return 0\n',
}
# a repository no graph was built for
PLAIN = {"tool.py": 'def retry_tool():\n    """Retry tool with backoff."""\n    return 2\n'}
NOTE = "# Note\n\n## Section\n" + "a note about retry and backoff in the vault " * 3


class WordEncoder:
    """Presence vector over VOCAB: deterministic, and a cosine that means
    'shares vocabulary' (see test_code_index_e2e)."""

    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), len(VOCAB)), dtype=np.float32)
        for i, text in enumerate(texts):
            low = str(text).lower()
            vec = np.array([1.0 if w in low else 0.0 for w in VOCAB], dtype=np.float32)
            norm = np.linalg.norm(vec)
            out[i] = vec / norm if norm else vec
        return out


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")


def _age(root: Path) -> None:
    """Older than the racy window, so the graph records a real mtime."""
    past = time.time() - 60
    for p in root.rglob("*"):
        if p.is_file() and ".git" not in p.parts:
            os.utime(p, (past, past))


def build_world(base: Path) -> Config:
    """A git repository with a graph, a plain one without, a vault note, and
    the memory and code indexes over them (the fake encoder must be in place)."""
    repos = base / "Project"
    _write(repos / "svc", SVC)
    _git(repos / "svc", "init", "-q")
    _git(repos / "svc", "add", "-A")
    _git(repos / "svc", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "c")
    _write(repos / "plain", PLAIN)
    _age(repos)
    vault = base / "vault"
    _write(vault, {"howto/n.md": NOTE})
    cfg = Config(vault=vault, data={"model": "fake", "min_score": 0.05, "excerpt_chars": 150,
                                    "code_repos_dir": str(repos), "code_graph_enabled": True})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    (svc,) = [u for u in scope.discover_units(cfg) if u.key == "svc"]
    build.build_unit(cfg, svc, workers=1)
    indexer_mod.rebuild(cfg, progress=False)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    return cfg


def engine_for(cfg: Config) -> SearchEngine:
    eng = SearchEngine.__new__(SearchEngine)
    eng.cfg = cfg
    eng.encoder = WordEncoder()
    eng.reload()
    return eng


# The shapes the golden holds: text and --json, without and with code.
GOLDEN_ARGV = [
    ["search", "retry backoff", "--top", "10"],
    ["search", "retry backoff", "--top", "10", "--json"],
    ["search", "retry backoff", "--top", "10", "--with-code"],
    ["search", "retry backoff", "--top", "10", "--with-code", "--json"],
    ["search", "retry worker", "--top", "6", "--code-only", "--max-per-file", "0"],
    ["search", "retry worker", "--top", "6", "--code-only", "--max-per-file", "0", "--explain", "--json"],
]


def run_search(cfg: Config, argv: list[str], respond, capsys) -> tuple[int, str, str, list[dict]]:
    """`mb search` through the real parser and command, the daemon replaced
    by `respond(payload)`. Returns (exit, stdout, stderr, payloads sent)."""
    sent: list[dict] = []

    def fake_search(cfg_, payload):
        sent.append(copy.deepcopy(payload))
        return respond(payload)

    saved = client.search
    client.search = fake_search
    try:
        code = cli.cmd_search(cfg, cli._build_parser().parse_args(argv))
    finally:
        client.search = saved
    out = capsys.readouterr()
    return code, out.out, out.err, sent


def _golden() -> list[dict]:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["cases"]


def _case_id(case: dict) -> str:
    return " ".join(case["argv"][1:])


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Built once for the tests that only read it."""
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: WordEncoder())
        yield build_world(tmp_path_factory.mktemp("world"))


@pytest.fixture
def own_world(tmp_path, monkeypatch):
    """A world a test may change."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: WordEncoder())
    return build_world(tmp_path)


def _graph_search(cfg, capsys, *extra) -> dict:
    """`--code-only --graph --json` over every chunk, keyed by (file, name)."""
    eng = engine_for(cfg)
    code, out, err, _ = run_search(cfg, ["search", "retry worker", "--top", "30", "--code-only", "--graph",
                                         "--json", "--max-per-file", "0", *extra], eng.search, capsys)
    assert code == 0, err
    return {(r["meta"]["file"], r["meta"]["name"]): r for r in json.loads(out)["results"]}


def _graph_db(cfg) -> Path:
    db = store.current_db(cfg, "svc")
    assert db is not None
    return db


# --- without --graph: the bytes of the baseline -------------------------------

@pytest.mark.parametrize("case", _golden(), ids=_case_id)
def test_without_graph_the_bytes_are_the_baseline(case, tmp_path, capsys, monkeypatch):
    """Planted: the JSON request made unconditional (`payload["format"] =
    "json"` outside `if graph`) -- every text case went red on its payload."""
    from singularity.codegraph import enrich

    def never(*_a, **_k):
        raise AssertionError("the graph was consulted without --graph")

    monkeypatch.setattr(enrich, "enrich", never)
    cfg = Config(vault=tmp_path, data={"code_repos_dir": str(tmp_path / "Project"), "code_graph_enabled": True})
    code, out, err, sent = run_search(cfg, case["argv"], lambda payload: copy.deepcopy(case["response"]), capsys)
    assert sent == [case["payload"]]                 # the daemon is asked exactly what it was asked
    assert (code, out, err) == (case["exit"], case["stdout"], case["stderr"])


@pytest.mark.parametrize("text_case, json_case", [(2, 3), (4, 5)])
def test_graph_text_is_the_baseline_plus_one_line_per_code_hit(text_case, json_case, tmp_path, capsys):
    """The text of `--graph` is rendered in the CLI from the JSON answer; take
    its graph lines away and the baseline's bytes remain. Planted: `:.3f` for
    the score in the CLI renderer -- red."""
    cases = _golden()
    text, answer = cases[text_case], cases[json_case]["response"]
    cfg = Config(vault=tmp_path, data={"code_repos_dir": str(tmp_path / "Project"), "code_graph_enabled": True})
    code, out, _, sent = run_search(cfg, [*text["argv"], "--graph"],
                                    lambda payload: copy.deepcopy(answer), capsys)
    assert code == 0 and sent[0]["format"] == "json"
    lines = out.splitlines(keepends=True)
    graph_lines = [ln for ln in lines if ln.startswith("   graph: ")]
    assert "".join(ln for ln in lines if not ln.startswith("   graph: ")) == text["stdout"]
    # one per code hit, none for a note: no graph was built here, so every code hit is outside it
    assert graph_lines == ["   graph: outside graph universe\n"] * sum(
        r["source"] == "code" for r in answer["results"])


def test_the_cli_line_renderer_is_the_engine_s():
    """`commands.memory._compact_line` is a copy (the engine imports numpy);
    a drift between the two would change `--graph` text only, silently."""
    from singularity.commands.memory import _compact_line
    items = [
        {"meta": {"file": "howto/a.md", "line": 12, "type": "howto", "project": "p", "title": "T",
                  "section": "S", "content": "body"}, "score": 0.81649, "source": "memory"},
        {"meta": {"file": "howto/a.md", "title": "T", "content": "x\ny"}, "score": 0.5, "source": "memory"},
        {"meta": {"file": "svc/a.py", "kind": "function", "name": "f", "content": "def f(): ...",
                  "line_start": 3, "line_end": 9}, "score": 0.335, "source": "code"},
        {"meta": {"file": ".mb/sources/x.md", "type": "source", "title": "X"}, "score": 1.0, "source": "sources"},
        {"meta": {}, "score": 0.0049, "source": "code"},
    ]
    for item in items:
        assert _compact_line(item) == SearchEngine._compact_line(item)


# --- with --graph: what the graph knows, and when it knows nothing ------------

def test_code_hits_carry_the_definition_they_fall_in_and_its_relations(world, capsys):
    """Planted: `definition_for` reduced to its "innermost covering" branch --
    the decorated method's chunk (lines 9-14: decorator above, blank lines
    below) lost `Worker.run`, red."""
    hits = _graph_search(world, capsys)
    helper = hits[("svc/app/core.py", "helper")]["graph"]
    assert helper == {"unit": "svc", "file": "app/core.py", "qname": "app/core.py::helper", "kind": "function",
                      "callers": 3, "unresolved_candidates": 0, "callees": 0, "unresolved_calls": 0,
                      "stale": False}
    run = hits[("svc/app/core.py", "run")]["graph"]
    assert (run["qname"], run["kind"]) == ("app/core.py::Worker.run", "method")
    # `x.run()` in guess is no edge, but it is a possible caller, and guess's own unplaced call
    assert (run["callers"], run["unresolved_candidates"]) == (1, 1)
    guess = hits[("svc/app/other.py", "guess")]["graph"]
    assert (guess["callees"], guess["unresolved_calls"]) == (0, 1)
    assert hits[("svc/app/core.py", "Worker")]["graph"]["qname"] == "app/core.py::Worker"


def test_module_level_code_has_no_counts_rather_than_zeros(world, capsys):
    """Planted: callers=0 for a hit in no definition -- red."""
    settings = _graph_search(world, capsys)[("svc/app/settings.py", "lines_1")]["graph"]
    assert settings["qname"] is None and settings["file"] == "app/settings.py"
    assert settings["callers"] is None and settings["callees"] is None
    assert settings["unresolved_candidates"] is None and "no definition covers lines 1-2" in settings["note"]


def test_a_file_the_graph_does_not_hold_is_outside_its_universe(world, capsys):
    """A gitignored file (in the code index, not in git's universe) and a
    repository no graph was built for. Planted: a missing `files` row answered
    with a symbol-less dict -- red."""
    hits = _graph_search(world, capsys)
    assert hits[("svc/gen/built.py", "generated_retry")]["graph"] == "outside graph universe"
    assert hits[("plain/tool.py", "retry_tool")]["graph"] == "outside graph universe"


def test_a_unit_whose_graph_cannot_be_read_says_so(own_world, capsys):
    """A unit's card with its generation gone is not "outside the universe"
    (the graph would hold the file) and not "0 callers": it is unavailable.
    Planted: the unavailable unit answered as OUTSIDE -- red."""
    cfg = own_world
    (store.unit_dir(cfg, "svc") / "CURRENT").unlink()
    helper = _graph_search(cfg, capsys)[("svc/app/core.py", "helper")]["graph"]
    assert helper["unit"] == "svc" and "no current generation" in helper["unavailable"]
    code, out, _, _ = run_search(cfg, ["search", "retry", "--code-only", "--graph"], engine_for(cfg).search, capsys)
    assert code == 0 and "   graph: svc: unavailable (" in out


def test_notes_get_no_graph_and_the_graph_is_closed_after(world, capsys, monkeypatch):
    """Every graph the search opened is closed when it returns (on Windows an
    open one holds the file: a later build's GC fails with WinError 32).
    Planted: `session.close()` removed -- red."""
    import sqlite3

    from singularity.codegraph import query
    opened = []
    real_open = query.open_card
    monkeypatch.setattr(query, "open_card", lambda cfg, card: opened.append(real_open(cfg, card)) or opened[-1])
    eng = engine_for(world)
    code, out, _, _ = run_search(world, ["search", "retry backoff", "--top", "10", "--with-code", "--graph",
                                         "--json"], eng.search, capsys)
    results = json.loads(out)["results"]
    assert code == 0 and any(r["source"] == "memory" for r in results)
    assert all(("graph" in r) == (r["source"] == "code") for r in results)
    assert [g.key for g in opened] == ["svc"]                    # one unit, opened once
    for g in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            g.con.execute("SELECT 1")


def test_a_file_the_index_and_the_graph_hold_in_different_versions_is_stale(own_world, capsys):
    """The index is updated past the graph (the graph is never refreshed by a
    search). Planted: `versions_differ` always None -- red."""
    cfg = own_world
    core = cfg.code_repos_dir / "svc" / "app" / "core.py"
    core.write_text("# moved down one line\n" + SVC["app/core.py"], encoding="utf-8")
    indexer_mod.update(cfg, corpus="code")
    hits = _graph_search(cfg, capsys)
    helper = hits[("svc/app/core.py", "helper")]["graph"]
    assert helper["stale"] is True
    assert helper["stale_reason"] == "the code index and the graph hold different versions of this file"
    assert hits[("svc/app/other.py", "use")]["graph"]["stale"] is False


def test_what_the_graph_itself_knows_is_stale_is_said(own_world, capsys):
    """`update.known_stale`: the stale set, then the whole-graph flag.
    Planted: the stale-set check dropped -- red."""
    import sqlite3
    cfg = own_world
    con = sqlite3.connect(_graph_db(cfg))
    with con:
        con.execute("INSERT INTO stale(path, reason) VALUES ('app/other.py', 'depends on a change')")
    hits = _graph_search(cfg, capsys)
    assert hits[("svc/app/other.py", "use")]["graph"]["stale_reason"] == "the graph marks this file stale"
    assert hits[("svc/app/core.py", "helper")]["graph"]["stale"] is False
    with con:
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('stale_all', ?)", (json.dumps("9000 files"),))
    con.close()
    hits = _graph_search(cfg, capsys)
    assert hits[("svc/app/core.py", "helper")]["graph"]["stale_reason"] == "the graph may be stale: 9000 files"


def test_a_racy_graph_stat_is_settled_by_the_file_on_disk(own_world, capsys):
    """A stat recorded as 0 (store.RACY_NS) cannot be joined by mtime; the
    disk decides. Planted: the disk arbitration removed -- the first answer
    went stale, red."""
    import sqlite3
    cfg = own_world
    con = sqlite3.connect(_graph_db(cfg))
    with con:
        con.execute("UPDATE files SET mtime_ns = 0 WHERE path = 'app/core.py'")
    con.close()
    assert _graph_search(cfg, capsys)[("svc/app/core.py", "helper")]["graph"]["stale"] is False
    core = cfg.code_repos_dir / "svc" / "app" / "core.py"
    core.write_text(SVC["app/core.py"].replace("return 1", "return 7"), encoding="utf-8")   # same size
    helper = _graph_search(cfg, capsys)[("svc/app/core.py", "helper")]["graph"]
    assert helper["stale_reason"] == ("cannot tell whether the code index and the graph hold the same "
                                      "version of this file")


# --- the join's two halves, pinned to the indexer they re-state --------------

def test_stat_ms_is_the_indexer_rounding(tmp_path):
    """`int(st_mtime * 1000)` on a double is not `mtime_ns // 10**6`: a
    double near 1.8e9 s resolves ~240 ns, so an mtime 100-200 ns short of a
    millisecond rounds up into it (NTFS keeps 100 ns). Planted: `stat_ms` as
    the integer division -- red on those cases."""
    import random

    from singularity.codegraph.enrich import stat_ms
    f = tmp_path / "f.py"
    f.write_text("x = 1\n", encoding="utf-8")
    sec = 1_790_000_000
    boundary = [ns for ns in (sec * 10**9 + k * 10**6 - d for k in range(1, 1000) for d in (100, 200))
                if int((ns // 10**9 + ns % 10**9 * 1e-9) * 1000) != ns // 10**6]
    assert boundary, "no mtime here rounds across a millisecond; the test proves nothing"
    rng = random.Random(7)
    samples = boundary[:50] + [rng.randrange(1_600_000_000, 1_900_000_000) * 10**9 + rng.randrange(10**9)
                               for _ in range(200)]
    for ns in samples:
        os.utime(f, ns=(ns, ns))
        st = os.stat(f)
        assert [stat_ms(st.st_mtime_ns), st.st_size] == indexer_mod._file_stat(f), ns


def test_the_index_generation_read_is_the_indexer_s(world, tmp_path, monkeypatch):
    """The numpy-free reader finds the file `indexer.index_files` finds and
    reads the same `file_meta`, from the head alone or by a whole parse.
    Planted: the head path returning `{}` -- red."""
    from singularity.codegraph import enrich
    from singularity.codegraph.enrich import committed_metadata, index_file_meta, parse_file_meta
    index_dir = world.code_index_dir
    assert committed_metadata(index_dir) == indexer_mod.index_files(index_dir)[1]
    text = committed_metadata(index_dir).read_text(encoding="utf-8")
    whole = json.loads(text)["file_meta"]
    assert whole and index_file_meta(index_dir) == whole
    # `model` first but `file_meta` not next: the fast path must not take the decoy
    reordered = json.dumps({"model": "m", "chunks": [{"file_meta": {"decoy": [0, 0]}}], "file_meta": whole})
    assert parse_file_meta(reordered) == whole
    # a torn CURRENT: both fall back to the newest whole generation
    copy_dir = tmp_path / "code_index"
    import shutil
    shutil.copytree(index_dir, copy_dir)
    (copy_dir / "CURRENT").write_text("gen_missing", encoding="utf-8")
    assert committed_metadata(copy_dir) == indexer_mod.index_files(copy_dir)[1]

    # Read in growing windows: a head longer than the first one, multi-byte
    # names cut by a window's edge, and a file of another shape.
    wide = {f"svc/{chr(233)}{chr(0x4E2D)}/{i}.py": [i, i] for i in range(300)}
    gen = tmp_path / "shapes" / "gen_1"
    gen.mkdir(parents=True)
    (gen / "embeddings.npy").write_bytes(b"")
    writer_shape = {"model": "m", "file_meta": wide, "chunks": [{"t": "x" * 5000}]}
    other_shape = {"model": "m", "chunks": [{"file_meta": {"decoy": [0, 0]}}] * 50, "file_meta": wide}
    whole_parse = enrich.parse_file_meta
    for shape, head_only in ((writer_shape, True), (other_shape, False)):
        (gen / "metadata.json").write_text(json.dumps(shape, ensure_ascii=False), encoding="utf-8")
        if head_only:       # the writer's shape never needs the whole file
            monkeypatch.setattr(enrich, "parse_file_meta", lambda text: pytest.fail("parsed whole"))
        for window in (3, 64, 1000, 1 << 17):
            monkeypatch.setattr(enrich, "_FIRST_READ", window)
            assert index_file_meta(tmp_path / "shapes") == wide, (window, head_only)
        monkeypatch.setattr(enrich, "parse_file_meta", whole_parse)


# --- with the boost --------------------------------------------------------------

def test_graph_and_the_graph_boost_work_together(world, capsys):
    """Both flags ask the daemon for JSON and both render the text lines; they
    were written apart (P7a, P7b). Together: the boost re-orders the hits, the
    enrichment then describes the same hits, and the text shows each graph line."""
    eng = engine_for(world)
    base = ["search", "retry worker", "--top", "30", "--code-only", "--max-per-file", "0", "--graph"]
    code, out, err, _ = run_search(world, [*base, "--graph-boost", "--json"], eng.search, capsys)
    assert code == 0, err
    both = json.loads(out)["results"]
    code, out, err, _ = run_search(world, [*base, "--json"], eng.search, capsys)
    alone = json.loads(out)["results"]

    def key(r):
        return r["meta"]["file"], r["meta"].get("name"), r["meta"].get("line")
    assert sorted(map(key, both)) == sorted(map(key, alone))           # re-ordered, never added or dropped
    assert all("graph" in r for r in both if r["source"] == "code")
    code, out, err, _ = run_search(world, [*base, "--graph-boost"], eng.search, capsys)
    assert code == 0, err
    assert out.count("   graph: ") == sum(1 for r in both if "graph" in r) > 0


# --- refusals and the import floor --------------------------------------------

def test_graph_is_refused_without_code_or_with_the_graph_off(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={"code_repos_dir": str(tmp_path), "code_graph_enabled": True})
    code, _, err, sent = run_search(cfg, ["search", "q", "--graph"], lambda p: {}, capsys)
    assert (code, sent) == (2, []) and "--with-code" in err
    cfg.data["code_graph_enabled"] = False
    code, _, err, sent = run_search(cfg, ["search", "q", "--with-code", "--graph"], lambda p: {}, capsys)
    assert (code, sent) == (2, []) and "code_graph_enabled" in err


def test_the_search_graph_path_never_imports_the_parser_or_the_model(world, tmp_path, capsys):
    """B2's floor for this path too: the whole `--graph` command runs in a
    fresh interpreter, the daemon replaced by a canned answer, and neither
    tree-sitter nor numpy nor sentence-transformers is loaded. Planted: `import
    numpy` at the top of codegraph/enrich.py -- red."""
    answer = engine_for(world).search({"query": "retry worker", "top_k": 30, "code_only": True,
                                       "max_per_file": 0, "format": "json"})
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"vault": str(world.vault), "data": world.data, "answer": answer}),
                    encoding="utf-8")
    probe = ("import contextlib, io, json, sys\n"
             "from pathlib import Path\n"
             "from singularity import cli\n"
             "from singularity.config import Config\n"
             "from singularity.search import client\n"
             "spec = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))\n"
             "client.search = lambda cfg, payload: spec['answer']\n"
             "cfg = Config(vault=Path(spec['vault']), data=spec['data'])\n"
             "buf = io.StringIO()\n"
             "with contextlib.redirect_stdout(buf):\n"
             "    code = cli.cmd_search(cfg, cli._build_parser().parse_args(\n"
             "        ['search', 'q', '--code-only', '--graph', '--json']))\n"
             "hits = json.loads(buf.getvalue())['results']\n"
             "print(json.dumps({'code': code,\n"
             "                  'described': sum(isinstance(h.get('graph'), dict) for h in hits),\n"
             "                  'loaded': sorted(m for m in ('tree_sitter', 'numpy', 'sentence_transformers')\n"
             "                                   if m in sys.modules)}))\n")
    done = subprocess.run([sys.executable, "-c", probe, str(spec)], capture_output=True, text=True,
                          cwd=str(REPO), env={**os.environ, "MB_CODE_NO_BACKGROUND": "1"})
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout)
    assert result["code"] == 0 and result["described"] >= 5      # the path really ran
    assert result["loaded"] == []


if __name__ == "__main__":
    # Writes the golden. Run ONLY with the code whose bytes are the baseline
    # (it was run once, at dec1d60): regenerating it with a later search
    # silently re-blesses whatever that search prints (D1 UNMET b).
    import contextlib
    import io
    import tempfile

    class _Capsys:
        def __init__(self):
            self.buf_out, self.buf_err = io.StringIO(), io.StringIO()

        def readouterr(self):
            out = type("C", (), {"out": self.buf_out.getvalue(), "err": self.buf_err.getvalue()})()
            self.buf_out.seek(0), self.buf_out.truncate(), self.buf_err.seek(0), self.buf_err.truncate()
            return out

    indexer_mod.shared_encoder = lambda *_a, **_k: WordEncoder()
    with tempfile.TemporaryDirectory() as tmp:
        world = build_world(Path(tmp))
        eng = engine_for(world)
        cases = []
        for argv in GOLDEN_ARGV:
            cap = _Capsys()
            responses: list[dict] = []

            def respond(payload, _eng=eng, _r=responses):
                _r.append(copy.deepcopy(_eng.search(payload)))
                return copy.deepcopy(_r[-1])

            with contextlib.redirect_stdout(cap.buf_out), contextlib.redirect_stderr(cap.buf_err):
                code, out, err, sent = run_search(world, argv, respond, cap)
            cases.append({"argv": argv, "exit": code, "payload": sent[0], "response": responses[0],
                          "stdout": out, "stderr": err})
    GOLDEN.write_text(json.dumps({"written_by": sys.argv[1] if len(sys.argv) > 1 else "?",
                                  "cases": cases}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"wrote {GOLDEN} ({len(cases)} cases)")
