"""W01: raw source registry + write-once storage.

Content-addressed ids, an atomic registry, a Windows-safe slug, versioning via
supersedes/superseded_by, and the invariant that matters most (D6): an already
stored raw document is NEVER rewritten.
"""
from __future__ import annotations

import json
import threading

import pytest

from singularity.knowledge import sources
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _add(cfg, text, **kw):
    kw.setdefault("origin_kind", "file")
    return sources.add(cfg, text, **kw)


# --- identity ---------------------------------------------------------------

def test_source_id_is_stable_and_whitespace_insensitive():
    a = sources.compute_source_id("# Title\n\nHello   world!")
    b = sources.compute_source_id("# Title\n\n\nHello world!\n")
    assert a == b, "normalization must collapse whitespace differences"
    assert len(a) == sources.SOURCE_ID_CHARS


def test_source_id_differs_for_different_content():
    assert sources.compute_source_id("alpha beta") != sources.compute_source_id("alpha gamma")


def test_punctuation_only_documents_do_not_collide():
    """normalize_gist() empties a punctuation-only document; without the raw
    fallback every such file would share one id."""
    assert sources.compute_source_id("!!!") != sources.compute_source_id("???")


def test_content_sha256_is_of_raw_text():
    import hashlib
    text = "Hello   world"
    assert sources.content_sha256(text) == hashlib.sha256(text.encode()).hexdigest()


# --- slug -------------------------------------------------------------------

@pytest.mark.parametrize("title, expect", [
    ("a/b", "a-b"),
    ("a\\b:c*d?e", "a-b-c-d-e"),
    ("  spaced   out  ", "spaced-out"),
    ("", "source"),
    ("...", "source"),
    ("trailing dot.", "trailing-dot"),
    ("CON", "_CON"),
    ("com1", "_com1"),
    ("nul.txt", "_nul.txt"),
])
def test_slugify_edge_cases(title, expect):
    assert sources.slugify(title) == expect


def test_slugify_truncates_and_keeps_unicode():
    slug = sources.slugify("Заголовок 🎉 " + "x" * 300)
    assert len(slug) <= sources.SLUG_MAX_CHARS
    assert slug.startswith("Заголовок-🎉")
    assert not slug.endswith(("-", ".", " "))


def test_slugified_names_are_writable_on_this_platform(tmp_path):
    for title in ("CON", "a/b", "x" * 300, "эмодзи 🎉 тест", "trailing dot.", "?<>|"):
        rel = sources.stored_relpath("ab12cd34ef56", title, "2026-07-28T10:00:00")
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("ok", encoding="utf-8")
        assert path.is_file()


# --- add / idempotency ------------------------------------------------------

def test_add_stores_registry_and_raw(cfg):
    result = _add(cfg, "# Doc\n\nbody text here", origin_ref="D:/docs/x.md")
    assert result["duplicate"] is False
    assert result["stored_path"].startswith(".mb/sources/")
    stored = sources.stored_abspath(cfg, result["stored_path"])
    assert stored.is_file()
    reg = json.loads(sources.registry_path(cfg).read_text(encoding="utf-8"))
    assert reg["schema_version"] == sources.REGISTRY_SCHEMA_VERSION
    rec = reg["sources"][result["source_id"]]
    assert rec["status"] == "new"
    assert rec["origin"] == {"kind": "file", "ref": "D:/docs/x.md"}
    assert rec["derived_notes"] == []
    assert rec["stored"] is True


def test_add_is_idempotent_and_touches_nothing(cfg):
    text = "# Doc\n\nrepeatable body"
    first = _add(cfg, text, origin_ref="ref")
    reg_path = sources.registry_path(cfg)
    stored = sources.stored_abspath(cfg, first["stored_path"])
    reg_before, raw_before = reg_path.read_bytes(), stored.read_bytes()

    second = _add(cfg, text, origin_ref="ref")
    assert second["duplicate"] is True
    assert second["source_id"] == first["source_id"]
    assert reg_path.read_bytes() == reg_before, "registry must be byte-identical"
    assert stored.read_bytes() == raw_before, "raw must be byte-identical"


def test_whitespace_variant_is_a_duplicate_not_a_second_file(cfg):
    _add(cfg, "# Doc\n\nsame body", origin_ref="ref")
    again = _add(cfg, "# Doc\n\n\nsame   body\n", origin_ref="ref")
    assert again["duplicate"] is True
    files = list((cfg.sources_dir).rglob("*.md"))
    assert len(files) == 1


def test_existing_raw_file_is_never_overwritten(cfg):
    """Registry lost but raw present: the record is recovered, the bytes on
    disk are left exactly as they were (write-once, D6)."""
    first = _add(cfg, "# Doc\n\nbody", origin_ref="ref")
    stored = sources.stored_abspath(cfg, first["stored_path"])
    stored.write_text("TAMPERED", encoding="utf-8")
    sources.registry_path(cfg).unlink()

    again = _add(cfg, "# Doc\n\nbody", origin_ref="ref")
    assert again["duplicate"] is True
    assert again.get("recovered") is True
    assert stored.read_text(encoding="utf-8") == "TAMPERED"
    assert sources.get(cfg, first["source_id"]) is not None


def test_raw_body_is_verbatim(cfg):
    text = "# T\n\nline1\n\n```\n  raw   spacing  |\n```\n- [[Link]] #tag\n"
    result = _add(cfg, text, origin_ref="ref")
    assert sources.read_source_text(cfg, result["source_id"]) == text


def test_stored_file_frontmatter_parses(cfg):
    from singularity.search.chunking import parse_frontmatter
    result = _add(cfg, "# T\n\nbody", origin_ref="ref", project="proj", tags=["a", "b"])
    raw = sources.stored_abspath(cfg, result["stored_path"]).read_text(encoding="utf-8")
    fm, _ = parse_frontmatter(raw)
    assert fm["type"] == "source"
    assert fm["source_id"] == result["source_id"]
    assert fm["immutable"] is True
    assert fm["origin"] == {"kind": "file", "ref": "ref"}
    assert fm["project"] == "proj"
    assert fm["tags"] == ["a", "b"]
    assert isinstance(fm["sha256"], str), "hex hash must stay a string, not become an int"


def test_all_digit_ids_stay_strings(cfg):
    """A source_id/sha256 that happens to be all digits must not be parsed
    back as a YAML integer."""
    from singularity.search.chunking import parse_frontmatter
    record = {"source_id": "123456789012", "title": "t", "sha256": "1234567890",
              "origin": {"kind": "file", "ref": "r"}, "bytes": 1, "chars": 1,
              "content_type": "text/markdown", "ingested_at": "2026-07-28T10:00:00"}
    fm, _ = parse_frontmatter(sources.render_source_file(record, "body"))
    assert fm["source_id"] == "123456789012"
    assert fm["sha256"] == "1234567890"


# --- versioning -------------------------------------------------------------

def test_changed_content_same_origin_creates_a_version_chain(cfg):
    v1 = _add(cfg, "# Doc\n\nversion one", origin_ref="D:/docs/x.md",
              now="2026-01-01T10:00:00")
    v2 = _add(cfg, "# Doc\n\nversion two", origin_ref="D:/docs/x.md",
              now="2026-02-01T10:00:00")
    assert v2["source_id"] != v1["source_id"]
    assert v2["supersedes"] == v1["source_id"]
    assert sources.get(cfg, v1["source_id"])["superseded_by"] == v2["source_id"]
    assert sources.get(cfg, v2["source_id"])["superseded_by"] is None


def test_version_chain_links_pairwise_not_to_the_oldest(cfg):
    v1 = _add(cfg, "one", origin_ref="ref", now="2026-01-01T10:00:00")
    v2 = _add(cfg, "two", origin_ref="ref", now="2026-02-01T10:00:00")
    v3 = _add(cfg, "three", origin_ref="ref", now="2026-03-01T10:00:00")
    assert v3["supersedes"] == v2["source_id"]
    assert sources.get(cfg, v1["source_id"])["superseded_by"] == v2["source_id"]


def test_different_origin_does_not_supersede(cfg):
    _add(cfg, "one", origin_ref="a", now="2026-01-01T10:00:00")
    other = _add(cfg, "two", origin_ref="b", now="2026-02-01T10:00:00")
    assert other["supersedes"] is None


# --- guards -----------------------------------------------------------------

def test_oversize_source_is_refused_with_both_numbers(cfg):
    cfg.data["ingest_max_source_chars"] = 100
    with pytest.raises(sources.SourceError) as exc:
        _add(cfg, "x" * 101, origin_ref="ref")
    msg = str(exc.value)
    assert "101" in msg and "100" in msg
    assert "ingest_max_source_chars" in msg
    assert not sources.registry_path(cfg).exists(), "nothing may be written on refusal"


def test_empty_source_is_refused(cfg):
    with pytest.raises(sources.SourceError):
        _add(cfg, "   \n\n  ", origin_ref="ref")


def test_unknown_origin_kind_is_refused(cfg):
    with pytest.raises(sources.SourceError):
        sources.add(cfg, "body", origin_kind="magic", origin_ref="ref")


def test_corrupt_registry_reads_tolerantly_but_refuses_to_be_clobbered(cfg):
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    sources.registry_path(cfg).write_text("{not json", encoding="utf-8")

    # read paths degrade quietly
    assert sources.read_registry(cfg)["sources"] == {}
    assert sources.list_sources(cfg) == []
    assert sources.get(cfg, "abc") is None

    # mutation refuses, actionably, and leaves the damaged file untouched
    with pytest.raises(sources.SourceError) as exc:
        _add(cfg, "body", origin_ref="ref")
    assert "unreadable" in str(exc.value)
    assert sources.registry_path(cfg).read_text(encoding="utf-8") == "{not json"


def test_registry_wrong_shape_is_treated_as_corrupt(cfg):
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    sources.registry_path(cfg).write_text('{"sources": []}', encoding="utf-8")
    with pytest.raises(sources.SourceError):
        _add(cfg, "body", origin_ref="ref")


def test_url_detection():
    assert sources.is_url("https://example.com/a")
    assert sources.is_url("HTTP://example.com")
    assert not sources.is_url("D:/docs/x.md")
    assert not sources.is_url("relative/path.md")


# --- dry run ----------------------------------------------------------------

def test_dry_run_writes_nothing(cfg):
    result = sources.add(cfg, "# T\n\nbody", origin_ref="ref", dry_run=True)
    assert result["dry_run"] is True
    assert result["source_id"]
    assert not sources.registry_path(cfg).exists()
    assert not cfg.sources_dir.exists()


def test_dry_run_reports_duplicate_of_existing(cfg):
    _add(cfg, "# T\n\nbody", origin_ref="ref")
    result = sources.add(cfg, "# T\n\nbody", origin_ref="ref", dry_run=True)
    assert result["duplicate"] is True


# --- listing ----------------------------------------------------------------

def test_list_sources_is_newest_first_and_filterable(cfg):
    a = _add(cfg, "one", origin_ref="a", now="2026-01-01T10:00:00")
    b = _add(cfg, "two", origin_ref="b", now="2026-02-01T10:00:00")
    ids = [r["source_id"] for r in sources.list_sources(cfg)]
    assert ids == [b["source_id"], a["source_id"]]
    assert len(sources.list_sources(cfg, status="new")) == 2
    assert sources.list_sources(cfg, status="processed") == []
    assert len(sources.list_sources(cfg, limit=1)) == 1


# --- export -----------------------------------------------------------------

def test_export_copies_raw_and_registry(cfg, tmp_path):
    result = _add(cfg, "# T\n\nbody", origin_ref="ref")
    dest = tmp_path / "out"
    out = sources.export(cfg, dest)
    assert out["exported"] == 1
    assert (dest / "sources.json").is_file()
    copied = list(dest.rglob("*.md"))
    assert len(copied) == 1
    # original still in place — export copies, never moves
    assert sources.stored_abspath(cfg, result["stored_path"]).is_file()


def test_export_refuses_non_empty_destination_without_force(cfg, tmp_path):
    _add(cfg, "body", origin_ref="ref")
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("x", encoding="utf-8")
    with pytest.raises(sources.SourceError):
        sources.export(cfg, dest)
    out = sources.export(cfg, dest, force=True)
    assert out["exported"] == 1
    assert (dest / "keep.txt").is_file()


def test_export_single_id(cfg, tmp_path):
    a = _add(cfg, "one", origin_ref="a")
    _add(cfg, "two", origin_ref="b")
    out = sources.export(cfg, tmp_path / "out", source_id=a["source_id"])
    assert out["exported"] == 1


def test_export_unknown_id_is_actionable(cfg, tmp_path):
    with pytest.raises(sources.SourceError):
        sources.export(cfg, tmp_path / "out", source_id="deadbeef")


# --- locking ----------------------------------------------------------------

def test_registry_lock_is_exclusive(cfg):
    with sources.registry_lock(cfg), pytest.raises(sources.RegistryLockedError):  # noqa: SIM117
        with sources.registry_lock(cfg, wait_seconds=0.0):
            pass


def test_registry_lock_loser_does_not_remove_the_holders_lock(cfg):
    with sources.registry_lock(cfg):  # noqa: SIM117
        with pytest.raises(sources.RegistryLockedError):  # noqa: SIM117
            with sources.registry_lock(cfg, wait_seconds=0.0):
                pass
        assert cfg.ingest_lock_dir.exists(), "loser must never reap the holder's lock"
    assert not cfg.ingest_lock_dir.exists()


def test_concurrent_adds_do_not_lose_entries(cfg):
    texts = [f"# Doc {i}\n\nbody number {i}" for i in range(12)]
    errors: list[BaseException] = []

    def worker(text):
        try:
            _add(cfg, text, origin_ref=f"ref{text[-1]}")
        except BaseException as exc:  # noqa: BLE001 — surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(t,)) for t in texts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, errors
    assert len(sources.read_registry(cfg)["sources"]) == len(texts)
    assert len(list(cfg.sources_dir.rglob("*.md"))) == len(texts)
