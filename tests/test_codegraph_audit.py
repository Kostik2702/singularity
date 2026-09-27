"""The secret audit of a built graph (CODE-GRAPH D1 / P6; CODE-INDEX D5, layer 3).

Planted each time: a key in a signature default and one in a docstring's first
line (the plan's acceptance), keys only the stored facts hold (a call's
receiver, a name-shaped string), a key the decorators column hides behind JSON
escaping, and a PEM key cut short by the 200-character signature. Each must
fail the audit and none may be printed; a clean graph must pass. The batching
that makes the audit affordable is pinned against a text-by-text reference.

The fake keys are assembled at run time from parts no detector matches, so
this file adds nothing for the audit of the engine's own graph to find.
"""
from __future__ import annotations

import json
import random
from dataclasses import astuple
from pathlib import Path

import pytest

from singularity import cli
from singularity.codegraph import audit, build, model, scope, store
from singularity.codegraph.scope import GraphError
from singularity.commands.code import cmd_code
from singularity.config import Config
from singularity.knowledge.redact import MASK

# Shapes the detector knows (knowledge.redact), realistic in length and alphabet.
GITHUB = "gh" + "p_" + "a1B2c3D4e5" * 4                  # github-token
AWS = "AK" + "IA" + "Q7ZT3M9KX2LPV4RD"                    # aws-access-key
OPENAI = "sk-" + "proj-" + "Tq8Zr2Lm5Xv9Bn3Kd7Wp4Hs6"     # openai-key
ASSIGNED = "Xk29fLmQ8vTz" + "01BwR7"                      # the value of `token=...` (assigned-secret)
PEM_BODY = "MIIEpAIBAAKCAQEA" + "x7Yh2Kq9Lm4Nv8Pz" * 12
BACKSLASH_N = chr(92) + "n"                               # the two characters a source file writes
PEM = ("-----BEGIN RSA " + "PRIVATE KEY-----" + BACKSLASH_N + PEM_BODY + BACKSLASH_N
       + "-----END RSA " + "PRIVATE KEY-----")
REF_ONLY = "gh" + "p_" + "Z9y8X7w6V5" * 4                 # a github-token held only as a name-shaped string
SECRETS = (GITHUB, AWS, OPENAI, ASSIGNED, PEM_BODY[:24], REF_ONLY)

CLEAN = {
    "pkg/__init__.py": "",
    "pkg/core.py": 'def helper(name="plain"):\n    """Adds one."""\n    return 1\n\n\n'
                   "def run():\n    return helper()\n",
}
PLANTED = {
    **CLEAN,
    # the plan's acceptance: a signature default and a docstring's first line
    "pkg/client.py": (f'def connect(token="{GITHUB}"):\n    return token\n\n\n'
                      f'def fetch():\n    """Signs requests with {AWS} for the upload bucket."""\n    return 1\n'),
    # only the stored facts hold these: a call's receiver, a name-shaped string
    "pkg/rx.py": ("import requests\n\n\n"
                  "def pull(url):\n"
                  f'    return requests.Session(headers={{"Authorization": "Bearer {OPENAI}"}}).get(url)\n\n\n'
                  f'ROUTES = {{"deploy": "{REF_ONLY}"}}\n'),
    # stored as a JSON list: `token=\"...\"` in the column, which the assignment rule misses
    "pkg/web.py": f'import functools\n\n\n@functools.lru_cache(token="{ASSIGNED}")\ndef cached():\n    return 1\n',
    # cut at 200 characters: BEGIN and key material, no END
    "pkg/keys.py": f'def load(key="{PEM}"):\n    return key\n',
}


def _repo(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    root = tmp_path / "Project" / name
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _cfg(tmp_path: Path) -> Config:
    return Config(vault=tmp_path / "vault", data={"code_repos_dir": str(tmp_path / "Project"),
                                                  "code_graph_enabled": True})


def _build(cfg: Config) -> dict[str, dict]:
    return {u.key: build.build_unit(cfg, u, workers=1) for u in scope.discover_units(cfg)}


def _db(cfg: Config, key: str) -> Path:
    return store.current_db(cfg, key)


def _run(cfg, capsys, *argv):
    code = cmd_code(cfg, cli._build_parser().parse_args(["code", *argv]))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture(autouse=True)
def _no_background(monkeypatch):
    monkeypatch.setenv("MB_CODE_NO_BACKGROUND", "1")


@pytest.fixture
def planted(tmp_path):
    _repo(tmp_path, "svc", PLANTED)
    cfg = _cfg(tmp_path)
    summary = _build(cfg)["svc"]
    return cfg, summary, audit.audit_db(_db(cfg, "svc"), keep=False)


def _where(report: dict) -> set[tuple[str, str, str]]:
    return {(f["qname"], f["field"], f["kind"]) for f in report["findings"]}


# --- the acceptance: a planted secret fails the audit, a clean graph passes it ---------

def test_a_key_in_a_signature_default_and_in_doc1_fails_the_audit(planted, capsys):
    cfg, summary, report = planted
    assert ("pkg/client.py::connect", "nodes.signature", "github-token") in _where(report)
    assert ("pkg/client.py::fetch", "nodes.doc1", "aws-access-key") in _where(report)
    assert summary["audit"]["findings"] == report["count"] > 0      # the build audited what it wrote
    code, out, _ = _run(cfg, capsys, "audit", "--json")
    assert code == 1 and json.loads(out)["findings"] == report["count"]


def test_a_clean_graph_passes_and_the_count_is_kept_in_meta(tmp_path, capsys):
    _repo(tmp_path, "svc", CLEAN)
    cfg = _cfg(tmp_path)
    assert _build(cfg)["svc"]["audit"] == {"findings": 0, "by_kind": {}, "recorded": True}
    code, out, _ = _run(cfg, capsys, "audit")
    assert code == 0 and "svc: 0 secret-shaped value(s)" in out
    con = store.connect_ro(_db(cfg, "svc"))
    rec = audit.recorded(store.read_meta(con))
    con.close()
    assert rec["findings"] == 0 and rec["at_seq"] == 0


# --- what else holds source text, and why it is scanned ---------------------------------

def test_keys_only_the_stored_facts_hold_are_found(planted):
    """A call's receiver is stored whole, and a name-shaped string literal is
    stored as a ref: neither is in a node column (see audit's module doc)."""
    _, _, report = planted
    assert ("pkg/rx.py::pull", "facts.calls.receiver", "openai-key") in _where(report)
    assert ("pkg/rx.py::<module>", "facts.refs", "github-token") in _where(report)


def test_a_key_behind_the_json_of_the_decorators_column_is_found(planted):
    _, _, report = planted
    assert ("pkg/web.py::cached", "nodes.decorators", "assigned-secret") in _where(report)


def test_a_pem_key_cut_short_by_the_signature_limit_is_found(planted):
    _, _, report = planted
    found = {(q, f, k) for q, f, k in _where(report) if q == "pkg/keys.py::load"}
    assert found == {("pkg/keys.py::load", "nodes.signature", "private-key-header")}
    con = store.connect_ro(_db(planted[0], "svc"))
    signature = con.execute("SELECT signature FROM nodes WHERE qname = 'pkg/keys.py::load'").fetchone()[0]
    con.close()
    assert "-----END" not in signature        # why redact's block rule alone cannot see it


# --- a finding never carries the value ---------------------------------------------------

def test_no_report_prints_a_secret(planted, capsys):
    cfg, _, report = planted
    assert all(MASK in f["excerpt"] for f in report["findings"])
    _, as_json, _ = _run(cfg, capsys, "audit", "--json")
    _, as_text, _ = _run(cfg, capsys, "audit")
    for secret in SECRETS:
        assert secret not in as_json and secret not in as_text
    assert "pkg/client.py::connect" in as_text


# --- the command -------------------------------------------------------------------------

def test_audit_covers_every_graph_or_the_projects_one_and_runs_while_the_graph_is_off(tmp_path, capsys):
    _repo(tmp_path, "svc", PLANTED)
    clean_root = _repo(tmp_path, "clean", CLEAN)
    cfg = _cfg(tmp_path)
    _build(cfg)
    cfg.data["code_graph_enabled"] = False            # a graph left on disk still holds what it holds
    code, out, _ = _run(cfg, capsys, "audit", "--json")
    assert code == 1 and {u["unit"] for u in json.loads(out)["units"]} == {"svc", "clean"}
    cfg.data["projects"] = {"clean": {"path": str(clean_root)}}
    code, out, _ = _run(cfg, capsys, "audit", "--project", "clean", "--json")
    assert code == 0 and [u["unit"] for u in json.loads(out)["units"]] == ["clean"]


def test_nothing_to_audit_is_not_a_clean_audit(tmp_path, capsys):
    code, _, err = _run(_cfg(tmp_path), capsys, "audit")
    assert code == 2 and "nothing to audit" in err


def test_a_graph_of_another_schema_is_refused_not_half_read(tmp_path, capsys):
    _repo(tmp_path, "svc", CLEAN)
    cfg = _cfg(tmp_path)
    _build(cfg)
    with store.write_transaction(_db(cfg, "svc")) as con:
        con.execute("UPDATE meta SET v = ? WHERE k = 'schema_version'", (json.dumps(store.SCHEMA_VERSION - 1),))
    with pytest.raises(GraphError, match="schema"):
        audit.audit_db(_db(cfg, "svc"))
    code, out, _ = _run(cfg, capsys, "audit")
    assert code == 2 and "not audited" in out


def test_a_stored_fact_the_audit_cannot_read_is_not_a_clean_result(tmp_path, capsys):
    _repo(tmp_path, "svc", CLEAN)
    cfg = _cfg(tmp_path)
    _build(cfg)
    with store.write_transaction(_db(cfg, "svc")) as con:
        con.execute("UPDATE facts SET blob = ? WHERE file_id = (SELECT min(file_id) FROM facts)", (b"not zlib",))
    with pytest.raises(GraphError, match="cannot be read"):
        audit.audit_db(_db(cfg, "svc"))
    code, out, _ = _run(cfg, capsys, "audit")
    assert code == 2 and "not audited" in out


def test_the_audit_reads_one_state_of_the_graph(tmp_path, monkeypatch):
    """An update committed while the audit runs is not half seen: the count
    is that of the state whose `seq` is kept with it."""
    _repo(tmp_path, "svc", CLEAN)
    cfg = _cfg(tmp_path)
    _build(cfg)
    db = _db(cfg, "svc")
    real = audit._scan_nodes

    def nodes_then_an_update(con, s):
        real(con, s)
        with store.write_transaction(db) as w:        # another process's update, committed mid-audit
            fid, blob = w.execute("SELECT file_id, blob FROM facts ORDER BY file_id LIMIT 1").fetchone()
            facts = model.decode_facts(blob)
            facts.refs.append(GITHUB)
            w.execute("UPDATE facts SET blob = ? WHERE file_id = ?", (model.encode_facts(facts), fid))
            w.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('seq', '1')")

    monkeypatch.setattr(audit, "_scan_nodes", nodes_then_an_update)
    report = audit.audit_db(db, keep=False)
    assert (report["seq"], report["count"]) == (0, 0)
    monkeypatch.setattr(audit, "_scan_nodes", real)
    report = audit.audit_db(db, keep=False)
    assert (report["seq"], report["count"]) == (1, 1)


def test_the_audit_command_keeps_its_count_for_doctor(planted, capsys):
    cfg, _, report = planted
    with store.write_transaction(_db(cfg, "svc")) as con:
        con.execute("DELETE FROM meta WHERE k = ?", (audit.RECORD_KEY,))
    _run(cfg, capsys, "audit")
    con = store.connect_ro(_db(cfg, "svc"))
    meta = store.read_meta(con)
    con.close()
    assert audit.recorded(meta)["findings"] == report["count"]
    assert meta.get("seq", 0) == 0                    # a record is not a change to the graph


# --- batching and the clean-text cache equal a text-by-text scan -------------------------

def _reference(items):
    """The audit without batches or cache: every text checked on its own."""
    out = []
    for qname, fld, text in items:
        kinds, masked = audit._check(text) if text else ([], "")
        out += [audit.Finding(qname, fld, k, audit._excerpt(masked)) for k in dict.fromkeys(kinds)]
    return _bag(out)


def _bag(findings) -> list[tuple]:
    return sorted(map(astuple, findings))


CR = chr(13)
CRAFTED = {
    # a match rejected as teaching that ran from one text into the next would hide the real one
    "rejected match across texts": ["example token", ": api_key=" + ASSIGNED],
    # a break other than the newline shifts redact's line lookup onto a teaching line
    "line count shifted": ["a" + CR + "b" + CR + "example", "api_key=" + ASSIGNED],
    "bearer then a token": ["Bearer", OPENAI[8:]],
    "key block split in two": ["-----BEGIN RSA PRIVATE KEY-----", "-----END RSA PRIVATE KEY-----"],
    "url split in two": ["https://svc:", "pw123456@host"],
    "a secret after clean texts": ["self", "os.path", "str", GITHUB],
}


@pytest.mark.parametrize("name", sorted(CRAFTED))
def test_a_batch_reports_exactly_what_its_texts_report_alone(name):
    items = [("m::<module>", "facts.refs", t) for t in CRAFTED[name]]
    assert _bag(audit.scan_items(items)) == _reference(items)


def test_random_batches_report_exactly_what_their_texts_report_alone():
    parts = ["token", "api_key", "password", "=", ": ", " ", "example", "Bearer ", "https://u:", "@h",
             "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----", CR, "\n", "self.x", "str",
             ASSIGNED, GITHUB, AWS, OPENAI, PEM_BODY[:32]]
    rng = random.Random(7)
    for _ in range(400):
        items = [(f"f{i}::<module>", "facts.refs", "".join(rng.choice(parts) for _ in range(rng.randint(1, 4))))
                 for i in range(rng.randint(1, 8))]
        assert _bag(audit.scan_items(items)) == _reference(items), items


def test_a_secret_is_reported_wherever_it_occurs_not_once():
    """The cache keeps texts found clean; a text with a finding never enters
    it. The filler puts the two occurrences in different batches: inside one
    dirty batch every text is checked again anyway."""
    filler = [("f.py::<module>", "facts.refs", f"name_{i}_" + "y" * 90) for i in range(2 * audit._BATCH_CHARS // 90)]
    items = [("a.py::<module>", "facts.refs", GITHUB), *filler, ("b.py::<module>", "facts.refs", GITHUB)]
    found = audit.scan_items(items)
    assert [f.qname for f in found] == ["a.py::<module>", "b.py::<module>"]
