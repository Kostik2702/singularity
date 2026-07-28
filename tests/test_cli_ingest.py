"""W02: `mb ingest add|list|show|export` CLI contract.

Covers the exit-code contract (0 ok / 0 duplicate / 1 i-o|state / 2 args|config),
the zero-side-effect guarantee of --dry-run, the network refusal (D4), and the
stable --json schema every skill and script will depend on.
"""
from __future__ import annotations

import io
import json

import pytest

from singularity import sources
from singularity.cli import main


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    monkeypatch.delenv("MB_VAULT", raising=False)
    return tmp_path


def _run(argv, capsys):
    code = main(argv)
    out = capsys.readouterr()
    return code, out.out, out.err


def _cfg(vault):
    from singularity.config import Config
    return Config(vault=vault, data={})


def _doc(tmp_path, name="doc.md", body="# Doc\n\nA reasonably long body line.\n"):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


# --- add --------------------------------------------------------------------

def test_add_stores_and_prints_id(vault, tmp_path, capsys):
    doc = _doc(tmp_path)
    code, out, err = _run(["ingest", "add", str(doc), "--json"], capsys)
    assert code == 0, err
    payload = json.loads(out)
    assert payload["duplicate"] is False
    assert payload["stored_path"].startswith(".mb/sources/")
    assert (vault / payload["stored_path"]).is_file()
    assert sources.get(_cfg(vault), payload["source_id"]) is not None


def test_json_schema_is_stable(vault, tmp_path, capsys):
    """Locked contract for skills/scripts. Extending it is allowed; removing or
    renaming a key is a breaking change and must fail here first."""
    doc = _doc(tmp_path)
    code, out, _ = _run(["ingest", "add", str(doc), "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert {"source_id", "duplicate", "stored_path", "stored", "title",
            "sha256", "bytes", "chars", "supersedes", "dry_run"} <= set(payload)
    assert isinstance(payload["source_id"], str) and len(payload["source_id"]) == 12
    assert isinstance(payload["duplicate"], bool)
    assert isinstance(payload["chars"], int)


def test_repeat_add_is_duplicate_exit_zero_and_disk_unchanged(vault, tmp_path, capsys):
    doc = _doc(tmp_path)
    code, out, _ = _run(["ingest", "add", str(doc), "--json"], capsys)
    first = json.loads(out)
    stored = vault / first["stored_path"]
    before = (stored.read_bytes(), (vault / ".mb" / "sources.json").read_bytes())

    code, out, _ = _run(["ingest", "add", str(doc), "--json"], capsys)
    assert code == 0
    assert json.loads(out)["duplicate"] is True
    assert (stored.read_bytes(), (vault / ".mb" / "sources.json").read_bytes()) == before


def test_dry_run_has_no_side_effects(vault, tmp_path, capsys):
    doc = _doc(tmp_path)
    code, out, _ = _run(["ingest", "add", str(doc), "--dry-run", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["dry_run"] is True and payload["source_id"]
    assert not (vault / ".mb" / "sources.json").exists()
    assert not (vault / ".mb" / "sources").exists()


def test_url_as_path_is_refused_with_the_working_command(vault, capsys):
    code, out, err = _run(["ingest", "add", "https://example.com/page"], capsys)
    assert code == 2
    assert "--stdin" in err and "--origin https://example.com/page" in err


def test_stdin_with_origin(vault, capsys, monkeypatch):
    class _Stdin:
        buffer = io.BytesIO("# Fetched\n\nremote body text".encode("utf-8"))
    monkeypatch.setattr("sys.stdin", _Stdin())
    code, out, err = _run(["ingest", "add", "--stdin", "--origin",
                           "https://example.com/x", "--json"], capsys)
    assert code == 0, err
    record = sources.get(_cfg(vault), json.loads(out)["source_id"])
    assert record["origin"] == {"kind": "stdin", "ref": "https://example.com/x"}


def test_path_and_stdin_together_is_an_argument_error(vault, tmp_path, capsys):
    code, _, err = _run(["ingest", "add", str(_doc(tmp_path)), "--stdin"], capsys)
    assert code == 2
    assert "not both" in err


def test_no_input_at_all_is_an_argument_error(vault, capsys):
    code, _, err = _run(["ingest", "add"], capsys)
    assert code == 2
    assert "nothing to ingest" in err


def test_missing_file_is_an_io_error(vault, tmp_path, capsys):
    code, _, err = _run(["ingest", "add", str(tmp_path / "nope.md")], capsys)
    assert code == 1
    assert "not a file" in err


def test_oversize_is_a_config_error(vault, tmp_path, capsys):
    (vault / ".mb").mkdir(parents=True, exist_ok=True)
    (vault / ".mb" / "config.json").write_text(
        json.dumps({"ingest_max_source_chars": 10}), encoding="utf-8")
    code, _, err = _run(["ingest", "add", str(_doc(tmp_path))], capsys)
    assert code == 2
    assert "ingest_max_source_chars" in err


def test_corrupt_registry_is_a_state_error(vault, tmp_path, capsys):
    (vault / ".mb").mkdir(parents=True, exist_ok=True)
    (vault / ".mb" / "sources.json").write_text("{broken", encoding="utf-8")
    code, _, err = _run(["ingest", "add", str(_doc(tmp_path))], capsys)
    assert code == 1
    assert "unreadable" in err


def test_title_and_tags_flow_into_the_registry(vault, tmp_path, capsys):
    code, out, _ = _run(["ingest", "add", str(_doc(tmp_path)), "--title", "Explicit",
                         "--tags", "a, b ,,c", "--json"], capsys)
    assert code == 0
    record = sources.get(_cfg(vault), json.loads(out)["source_id"])
    assert record["title"] == "Explicit"
    assert record["tags"] == ["a", "b", "c"]


def test_unknown_project_selector_is_an_argument_error(vault, tmp_path, capsys):
    code, _, err = _run(["ingest", "add", str(_doc(tmp_path)), "--project", "nope"], capsys)
    assert code == 2


# --- list -------------------------------------------------------------------

def test_list_json_and_status_filter(vault, tmp_path, capsys):
    _run(["ingest", "add", str(_doc(tmp_path, "a.md", "# A\n\nfirst document body")),
          "--json"], capsys)
    _run(["ingest", "add", str(_doc(tmp_path, "b.md", "# B\n\nsecond document body")),
          "--json"], capsys)
    code, out, _ = _run(["ingest", "list", "--json"], capsys)
    assert code == 0
    assert len(json.loads(out)["sources"]) == 2
    code, out, _ = _run(["ingest", "list", "--status", "processed", "--json"], capsys)
    assert json.loads(out)["sources"] == []


def test_list_human_output_when_empty(vault, capsys):
    code, out, _ = _run(["ingest", "list"], capsys)
    assert code == 0 and "(no sources)" in out


# --- show -------------------------------------------------------------------

def test_show_does_not_dump_the_whole_document(vault, tmp_path, capsys):
    body = "# Long\n\n" + ("word " * 2000)
    code, out, _ = _run(["ingest", "add", str(_doc(tmp_path, "l.md", body)), "--json"], capsys)
    sid = json.loads(out)["source_id"]

    code, out, _ = _run(["ingest", "show", sid], capsys)
    assert code == 0
    assert len(out) < 1000, "default show must be bounded by excerpt_chars"

    code, out, _ = _run(["ingest", "show", sid, "--excerpt", "5000", "--json"], capsys)
    payload = json.loads(out)
    assert payload["excerpt_chars"] == 5000
    assert len(payload["excerpt"]) > 1000


def test_show_unknown_id(vault, capsys):
    code, _, err = _run(["ingest", "show", "deadbeefcafe"], capsys)
    assert code == 2 and "unknown source_id" in err


# --- export -----------------------------------------------------------------

def test_export_copies_out_of_mb(vault, tmp_path, capsys):
    code, out, _ = _run(["ingest", "add", str(_doc(tmp_path)), "--json"], capsys)
    stored = vault / json.loads(out)["stored_path"]
    dest = tmp_path / "escape"
    code, out, _ = _run(["ingest", "export", str(dest), "--json"], capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["exported"] == 1
    assert (dest / "sources.json").is_file()
    assert stored.is_file(), "export copies, never moves"


# --- index / kill-switch ----------------------------------------------------

def test_ingest_index_refused_when_disabled(vault, capsys):
    (vault / ".mb").mkdir(parents=True, exist_ok=True)
    (vault / ".mb" / "config.json").write_text(
        json.dumps({"ingest_index_sources": False}), encoding="utf-8")
    code, _, err = _run(["ingest", "index"], capsys)
    assert code == 2 and "ingest_index_sources" in err


def test_search_with_sources_refused_when_disabled(vault, capsys):
    (vault / ".mb").mkdir(parents=True, exist_ok=True)
    (vault / ".mb" / "config.json").write_text(
        json.dumps({"ingest_index_sources": False}), encoding="utf-8")
    code, _, err = _run(["search", "anything", "--with-sources"], capsys)
    assert code == 2 and "ingest_index_sources" in err


def test_search_payload_carries_include_sources(vault, capsys, monkeypatch):
    """End-to-end plumbing of the flag: cli -> payload -> client."""
    from singularity.search import client
    seen = {}
    monkeypatch.setattr(client, "search",
                        lambda cfg, payload: seen.update(payload) or {"lines": []})
    code, _, _ = _run(["search", "q", "--with-sources"], capsys)
    assert code == 0 and seen["include_sources"] is True

    seen.clear()
    _run(["search", "q"], capsys)
    assert seen["include_sources"] is False


def test_search_prints_sources_warning(vault, capsys, monkeypatch):
    from singularity.search import client
    monkeypatch.setattr(client, "search", lambda cfg, payload: {
        "lines": [], "sources_warning": "model mismatch blah"})
    code, _, err = _run(["search", "q", "--with-sources"], capsys)
    assert code == 0 and "model mismatch blah" in err


def test_ingest_add_reports_pending_when_daemon_is_cold(vault, tmp_path, capsys, monkeypatch):
    from singularity.search import client
    monkeypatch.setattr(client, "ping", lambda cfg: None)
    code, out, _ = _run(["ingest", "add", str(_doc(tmp_path)), "--json"], capsys)
    assert code == 0 and json.loads(out)["indexed"] == "pending"


def test_ingest_add_indexes_through_a_warm_daemon(vault, tmp_path, capsys, monkeypatch):
    from singularity.search import client
    seen = {}
    monkeypatch.setattr(client, "ping", lambda cfg: {"status": "ok"})
    monkeypatch.setattr(client, "run_index", lambda cfg, action, files=None, corpus=None:
                        seen.update(action=action, files=files, corpus=corpus) or {})
    code, out, _ = _run(["ingest", "add", str(_doc(tmp_path)), "--json"], capsys)
    payload = json.loads(out)
    assert code == 0 and payload["indexed"] == "indexed"
    assert seen["corpus"] == "sources"
    assert seen["files"] == [payload["stored_path"]]


def test_ingest_add_marks_indexed_skipped_on_duplicate_and_dry_run(vault, tmp_path, capsys, monkeypatch):
    from singularity.search import client
    monkeypatch.setattr(client, "ping", lambda cfg: {"status": "ok"})
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None, corpus=None: {})
    doc = _doc(tmp_path)
    _run(["ingest", "add", str(doc), "--json"], capsys)
    _, out, _ = _run(["ingest", "add", str(doc), "--json"], capsys)
    assert json.loads(out)["indexed"] == "skipped"
    _, out, _ = _run(["ingest", "add", str(doc), "--dry-run", "--json"], capsys)
    assert json.loads(out)["indexed"] == "skipped"


def test_export_refuses_non_empty_destination(vault, tmp_path, capsys):
    _run(["ingest", "add", str(_doc(tmp_path)), "--json"], capsys)
    dest = tmp_path / "escape"
    dest.mkdir()
    (dest / "x.txt").write_text("keep", encoding="utf-8")
    code, _, err = _run(["ingest", "export", str(dest)], capsys)
    assert code == 2 and "not empty" in err
    code, _, _ = _run(["ingest", "export", str(dest), "--force", "--json"], capsys)
    assert code == 0
