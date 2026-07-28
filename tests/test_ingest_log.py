"""W06: `mb ingest mark` / `mb ingest log` + the .mb/ingest-log.jsonl history.

Without a journal the planner would propose the same pages forever and
"what turned into what" would exist only in someone's head. The two properties
that matter: a handled page stops resurfacing, and a re-ingested (re-hashed)
document brings it back.
"""
from __future__ import annotations

import json
import threading

import numpy as np
import pytest

from singularity import ingest_plan, sources
from singularity.cli import main
from singularity.config import Config
from singularity.search import indexer


class FakeEncoder:
    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.random(8).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


SHARED = "векторный поиск индексатор корпус эмбеддинги косинус "


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    for rel in ("reference/search.md", "reference/indexing.md", "howto/rebuild.md"):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: reference\ncreated: 2026-01-01\ntitle: {rel}\n---\n\n"
                     f"# {rel}\n\n## Раздел\n" + SHARED * 4, encoding="utf-8")
    c = Config(vault=vault, data={"model": "fake", "ingest_fanout_threshold": -1.0})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


def _ingest(cfg, text=None, **kw):
    kw.setdefault("origin_ref", "D:/docs/a.md")
    return sources.add(cfg, text or ("# Источник\n\n## Про поиск\n" + SHARED * 5), **kw)


def _build(cfg):
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")


# --- suppression ------------------------------------------------------------

def test_marked_pages_stop_resurfacing(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    before = [t["file"] for t in ingest_plan.plan(cfg, sid)["touch"]]
    assert "reference/search.md" in before

    sources.attach_notes(cfg, sid, ["reference/search.md"], status="partial", log=True)
    after = ingest_plan.plan(cfg, sid)
    assert "reference/search.md" not in [t["file"] for t in after["touch"]]
    assert after["suppressed"] == 1


def test_reingesting_a_changed_document_brings_the_pages_back(cfg):
    """A changed source is a new content hash, hence a new source_id — the old
    marks belong to the old version and must not silence the new one."""
    first = _ingest(cfg)
    _build(cfg)
    sources.attach_notes(cfg, first["source_id"], ["reference/search.md"], log=True)

    second = _ingest(cfg, text="# Источник\n\n## Про поиск\n" + SHARED * 5 + " добавлено")
    assert second["source_id"] != first["source_id"]
    assert second["supersedes"] == first["source_id"]
    indexer.rebuild(cfg, progress=False, corpus="sources")

    files = [t["file"] for t in ingest_plan.plan(cfg, second["source_id"])["touch"]]
    assert "reference/search.md" in files


def test_a_stale_mark_stamp_does_not_suppress(cfg):
    """Direct check of the sha binding, independent of id derivation."""
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    sources.attach_notes(cfg, sid, ["reference/search.md"], log=True)
    assert ingest_plan.plan(cfg, sid)["suppressed"] == 1

    registry = sources.read_registry(cfg)
    registry["sources"][sid]["marks"]["reference/search.md"]["sha256"] = "stale"
    sources.write_registry(cfg, registry)
    out = ingest_plan.plan(cfg, sid)
    assert out["suppressed"] == 0
    assert "reference/search.md" in [t["file"] for t in out["touch"]]


def test_marked_notes_helper():
    record = {"sha256": "abc", "marks": {"a.md": {"sha256": "abc"},
                                         "b.md": {"sha256": "old"},
                                         "c.md": "not-a-dict"}}
    assert sources.marked_notes(record) == {"a.md"}
    assert sources.marked_notes({"sha256": "x"}) == set()


# --- journal ----------------------------------------------------------------

def test_log_row_format_is_stable(cfg):
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, ["reference/a.md", "reference/b.md"],
                         status="processed", actor="skill", log=True,
                         now="2026-07-28T18:45:00")
    raw = sources.log_path(cfg).read_text(encoding="utf-8").strip()
    assert raw.count("\n") == 0, "one JSON object per line"
    row = json.loads(raw)
    assert row == {"ts": "2026-07-28T18:45:00", "op": "ingest", "source_id": sid,
                   "title": "Источник", "notes": ["reference/a.md", "reference/b.md"],
                   "status": "processed", "actor": "skill"}


def test_attach_notes_does_not_log_unless_asked(cfg):
    """`mb note new --from-source` writes a back-link, not a completed cycle."""
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, ["reference/a.md"])
    assert not sources.log_path(cfg).exists()


def test_nothing_to_add_is_a_recorded_outcome(cfg):
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, [], status="processed", log=True)
    rows = sources.read_log(cfg)
    assert len(rows) == 1 and rows[0]["notes"] == [] and rows[0]["status"] == "processed"


def test_mark_tolerates_a_deleted_note(cfg):
    sid = _ingest(cfg)["source_id"]
    out = sources.attach_notes(cfg, sid, ["reference/never-existed.md"], log=True)
    assert out["derived_notes"] == ["reference/never-existed.md"]


def test_read_log_filters(cfg):
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, ["a.md"], log=True, now="2026-01-01T10:00:00")
    sources.attach_notes(cfg, sid, ["b.md"], log=True, now="2026-06-01T10:00:00",
                         op="lint")
    assert len(sources.read_log(cfg)) == 2
    assert len(sources.read_log(cfg, since="2026-03-01")) == 1
    assert len(sources.read_log(cfg, op="lint")) == 1
    assert sources.read_log(cfg, limit=1)[0]["ts"] == "2026-06-01T10:00:00"


def test_read_log_skips_damaged_lines(cfg):
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, ["a.md"], log=True)
    with open(sources.log_path(cfg), "a", encoding="utf-8") as fh:
        fh.write("{not json\n[]\n")
    assert len(sources.read_log(cfg)) == 1


def test_missing_log_reads_as_empty(cfg):
    assert sources.read_log(cfg) == []


# --- retention --------------------------------------------------------------

def test_sweep_drops_rows_past_retention(cfg):
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, ["old.md"], log=True, now="2020-01-01T00:00:00")
    sources.attach_notes(cfg, sid, ["new.md"], log=True)
    out = sources.sweep_log(cfg)
    assert out == {"kept": 1, "swept": 1}
    assert [r["notes"] for r in sources.read_log(cfg)] == [["new.md"]]


def test_sweep_is_a_no_op_when_nothing_expired(cfg):
    sid = _ingest(cfg)["source_id"]
    sources.attach_notes(cfg, sid, ["a.md"], log=True)
    before = sources.log_path(cfg).read_bytes()
    assert sources.sweep_log(cfg) == {"kept": 1, "swept": 0}
    assert sources.log_path(cfg).read_bytes() == before


def test_sweep_on_a_missing_log(cfg):
    assert sources.sweep_log(cfg) == {"kept": 0, "swept": 0}


# --- concurrency ------------------------------------------------------------

def test_concurrent_marks_do_not_tear_the_journal(cfg):
    sid = _ingest(cfg)["source_id"]
    errors: list[BaseException] = []

    def worker(i):
        try:
            sources.attach_notes(cfg, sid, [f"reference/n{i}.md"], log=True)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    rows = sources.read_log(cfg)
    assert len(rows) == 10, "every mark must survive the read-modify-write"
    assert len(sources.get(cfg, sid)["derived_notes"]) == 10


# --- CLI --------------------------------------------------------------------

def test_cli_mark_and_log_round_trip(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    sid = _ingest(cfg)["source_id"]

    assert main(["ingest", "mark", sid, "--notes", "reference/a.md,reference/b.md",
                 "--actor", "skill", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "processed" and len(payload["added"]) == 2

    assert main(["ingest", "log", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)["rows"]
    assert len(rows) == 1 and rows[0]["actor"] == "skill"


def test_cli_mark_without_notes_records_the_empty_outcome(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    sid = _ingest(cfg)["source_id"]
    assert main(["ingest", "mark", sid]) == 0
    assert "nothing to add" in capsys.readouterr().out
    assert sources.read_log(cfg)[0]["notes"] == []


def test_cli_mark_unknown_source_is_an_argument_error(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    assert main(["ingest", "mark", "deadbeefcafe"]) == 2
    assert "unknown source_id" in capsys.readouterr().err


def test_cli_log_empty(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    assert main(["ingest", "log"]) == 0
    assert "(empty log)" in capsys.readouterr().out
