"""W10: migrations — retro-provenance, config v1->v2, and backup coverage of
the ONE non-reconstructible directory in the vault.

The headline test is `test_full_disaster_cycle`: back up, delete
`.mb/sources/` the way a "just nuke .mb and re-index" would, restore, and get
every byte back. That scenario is the entire reason decision D1 needed
compensations.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from singularity import backup, sources
from singularity.cli import main
from singularity.config import (DEFAULT_CONFIG, SCHEMA_VERSION, Config,
                                migrate_config, validate_config)


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


def _note(vault, rel, body="", fm=""):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: reference\ntitle: \"{p.stem}\"\ncreated: 2026-01-01\n"
                 f"{fm}---\n\n# {p.stem}\n\n{body}\n", encoding="utf-8")
    return p


# --- 10.1 backfill ----------------------------------------------------------

def test_frontmatter_source_url_is_registered(cfg):
    _note(cfg.vault, "reference/a.md", fm='source: "https://example.com/article"\n')
    dry = sources.backfill(cfg)
    assert dry["created"] == 1 and dry["applied"] is False
    assert not sources.registry_path(cfg).exists(), "a dry run writes nothing"

    applied = sources.backfill(cfg, apply=True)
    assert applied["created"] == 1
    record = sources.get(cfg, applied["entries"][0]["source_id"])
    assert record["origin"] == {"kind": "external", "ref": "https://example.com/article"}
    assert record["stored"] is False
    assert record["derived_notes"] == ["reference/a.md"]
    assert record["status"] == "processed", \
        "the notes already exist — leaving these 'new' would flood curator gaps"


def test_prose_sources_are_not_registered(cfg):
    """`source: "брейншторм + grill, сессия 2026-07-03"` is real provenance but
    not a retrievable document."""
    _note(cfg.vault, "reference/a.md", fm='source: "брейншторм + grill, сессия"\n')
    assert sources.backfill(cfg)["created"] == 0


def test_absolute_paths_count_as_external(cfg):
    _note(cfg.vault, "reference/a.md", fm='source: "D:/docs/spec.md"\n')
    assert sources.backfill(cfg)["created"] == 1


def test_urls_in_reference_bodies_are_registered(cfg):
    _note(cfg.vault, "reference/a.md", body="см. https://example.com/x и (https://b.dev/y).")
    result = sources.backfill(cfg, apply=True)
    refs = {e["ref"] for e in result["entries"]}
    assert refs == {"https://example.com/x", "https://b.dev/y"}, \
        "trailing punctuation must not become part of the URL"


def test_bodies_outside_reference_are_not_scanned(cfg):
    _note(cfg.vault, "howto/a.md", body="см. https://example.com/x")
    assert sources.backfill(cfg)["created"] == 0


def test_backfill_is_idempotent(cfg):
    _note(cfg.vault, "reference/a.md", fm='source: "https://example.com/article"\n')
    sources.backfill(cfg, apply=True)
    before = sources.registry_path(cfg).read_bytes()
    second = sources.backfill(cfg, apply=True)
    assert (second["created"], second["updated"]) == (0, 0)
    assert sources.registry_path(cfg).read_bytes() == before


def test_a_second_note_citing_the_same_ref_updates_the_record(cfg):
    _note(cfg.vault, "reference/a.md", fm='source: "https://example.com/x"\n')
    sources.backfill(cfg, apply=True)
    _note(cfg.vault, "reference/b.md", fm='source: "https://example.com/x"\n')
    result = sources.backfill(cfg, apply=True)
    assert result["updated"] == 1 and result["created"] == 0
    sid = sources.external_source_id("https://example.com/x")
    assert sources.get(cfg, sid)["derived_notes"] == ["reference/a.md", "reference/b.md"]


def test_external_ids_are_stable_and_case_insensitive():
    assert sources.external_source_id("https://X.com/A ") == \
        sources.external_source_id("https://x.com/a")


def test_url_cap_per_note_is_reported(cfg):
    urls = " ".join(f"https://example.com/{i}" for i in range(30))
    _note(cfg.vault, "reference/a.md", body=urls)
    result = sources.backfill(cfg)
    assert result["created"] == sources.BACKFILL_MAX_URLS_PER_NOTE
    assert result["capped_notes"] == ["reference/a.md"]


def test_backfill_does_not_touch_real_ingested_sources(cfg):
    real = sources.add(cfg, "# Doc\n\nbody", origin_ref="https://example.com/x")
    _note(cfg.vault, "reference/a.md", fm='source: "https://example.com/x"\n')
    sources.backfill(cfg, apply=True)
    assert sources.get(cfg, real["source_id"])["stored"] is True


def test_cli_backfill_dry_run_and_apply(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    _note(cfg.vault, "reference/a.md", fm='source: "https://example.com/article"\n')

    assert main(["ingest", "backfill", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert not sources.registry_path(cfg).exists()

    assert main(["ingest", "backfill", "--apply", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["created"] == 1


# --- 10.2 config v1 -> v2 ---------------------------------------------------

def test_schema_version_is_two():
    assert SCHEMA_VERSION == 2


def test_v1_config_migrates_without_touching_user_values():
    old = {"schema_version": 1, "min_score": 0.42, "batch_size": 8,
           "personal_roots": ["private"]}
    migrated, changed = migrate_config(old)
    assert changed is True
    assert migrated["schema_version"] == 2
    assert migrated["min_score"] == 0.42 and migrated["batch_size"] == 8
    assert migrated["personal_roots"] == ["private"]


def test_v1_config_produces_no_warnings():
    """Acceptance: an old config loads with no errors AND no warnings."""
    result = validate_config({"schema_version": 1, "min_score": 0.3})
    assert result["errors"] == [] and result["warnings"] == []


def test_every_new_key_has_a_default_and_validation():
    new_keys = ["ingest_index_sources", "ingest_max_source_chars",
                "ingest_fanout_top_k", "ingest_fanout_threshold",
                "ingest_log_retention_days", "immutable_roots",
                "gap_link_min_inbound", "gap_learning_age_days",
                "lint_llm_adjudication", "backup_max_sources_mb",
                "encoder_device"]
    for key in new_keys:
        assert key in DEFAULT_CONFIG, f"{key} has no default"
        assert validate_config({key: DEFAULT_CONFIG[key]})["warnings"] == [], \
            f"{key} is not in the known set"


@pytest.mark.parametrize("key, bad", [
    ("ingest_index_sources", "yes"),
    ("ingest_max_source_chars", 0),
    ("ingest_fanout_top_k", 0),
    ("ingest_fanout_threshold", 2.0),
    ("ingest_log_retention_days", 0),
    ("immutable_roots", "not-a-list"),
    ("gap_link_min_inbound", 0),
    ("gap_learning_age_days", 0),
    ("lint_llm_adjudication", 1),
    ("backup_max_sources_mb", 0),
    ("encoder_device", 5),
])
def test_new_keys_reject_bad_values(key, bad):
    assert validate_config({key: bad})["errors"], f"{key}={bad!r} must be rejected"


def test_migration_backs_up_the_old_config(tmp_path, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    (tmp_path / ".mb").mkdir(parents=True)
    (tmp_path / ".mb" / "config.json").write_text(
        json.dumps({"schema_version": 1, "min_score": 0.42}), encoding="utf-8")

    from singularity.config import load_config
    cfg = load_config(None)
    assert cfg.get("schema_version") == 2 and cfg.get("min_score") == 0.42
    assert list((tmp_path / ".mb").glob("config.json.bak-*")), "no .bak taken"


# --- 10.4 backup / restore of the ingest layer ------------------------------

def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_backup_includes_registry_and_raw(cfg):
    sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")
    manifest = backup.create(cfg, now="20260728-000000")
    assert manifest["sources_included"] is True
    assert manifest["sources_count"] == 1
    assert "sources.json" in manifest["files"]
    assert any(name.startswith("sources/") for name in manifest["files"])
    assert backup.verify(cfg, "20260728-000000")["ok"] is True


def test_verify_detects_a_corrupted_raw_copy(cfg):
    sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")
    backup.create(cfg, now="20260728-000001")
    copied = next((cfg.mb_dir / "backups" / "20260728-000001" / "sources").rglob("*.md"))
    copied.write_text("tampered", encoding="utf-8")
    result = backup.verify(cfg, "20260728-000001")
    assert result["ok"] is False and result["corrupt"]


def test_full_disaster_cycle(cfg):
    """create -> delete .mb/sources/ -> restore. The exact scenario D1's risk
    R8 is about: `.mb/` looks like disposable runtime state, and deleting it
    used to destroy the only copy of every ingested document."""
    import shutil

    first = sources.add(cfg, "# One\n\nfirst external body", origin_ref="a")
    second = sources.add(cfg, "# Two\n\nsecond external body", origin_ref="b")
    before = {rel: _sha(sources.stored_abspath(cfg, rel))
              for rel in (first["stored_path"], second["stored_path"])}

    backup.create(cfg, now="20260728-000002")
    assert backup.verify(cfg, "20260728-000002")["ok"] is True

    shutil.rmtree(cfg.sources_dir)
    sources.registry_path(cfg).unlink()
    assert sources.list_sources(cfg) == []

    result = backup.restore(cfg, "20260728-000002")
    assert result["ok"] is True and result["sources_restored"] == 2
    assert result["registry_entries_added"] == 2
    after = {rel: _sha(sources.stored_abspath(cfg, rel)) for rel in before}
    assert after == before, "restored bytes must be identical"
    assert {r["source_id"] for r in sources.list_sources(cfg)} == \
        {first["source_id"], second["source_id"]}


def test_restore_never_overwrites_an_existing_source(cfg):
    added = sources.add(cfg, "# One\n\nbody", origin_ref="a")
    backup.create(cfg, now="20260728-000003")
    target = sources.stored_abspath(cfg, added["stored_path"])
    target.write_text("LOCAL VERSION", encoding="utf-8")

    result = backup.restore(cfg, "20260728-000003")
    assert result["sources_restored"] == 0 and result["sources_kept"] == 1
    assert target.read_text(encoding="utf-8") == "LOCAL VERSION"


def test_restore_merges_the_registry_without_dropping_local_entries(cfg):
    old = sources.add(cfg, "# Old\n\nbody one", origin_ref="a")
    backup.create(cfg, now="20260728-000004")
    new = sources.add(cfg, "# New\n\nbody two", origin_ref="b")

    backup.restore(cfg, "20260728-000004")
    ids = {r["source_id"] for r in sources.list_sources(cfg)}
    assert ids == {old["source_id"], new["source_id"]}


def test_oversize_raw_is_skipped_with_a_warning(cfg):
    cfg.data["backup_max_sources_mb"] = 0.000001
    sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")
    manifest = backup.create(cfg, now="20260728-000005")
    assert manifest["sources_included"] is False
    assert manifest["warnings"] and "backup_max_sources_mb" in manifest["warnings"][0]
    assert not any(name.startswith("sources/") for name in manifest["files"])


def test_with_sources_forces_inclusion(cfg):
    cfg.data["backup_max_sources_mb"] = 0.000001
    sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")
    manifest = backup.create(cfg, now="20260728-000006", with_sources=True)
    assert manifest["sources_included"] is True


def test_no_sources_flag_excludes_them(cfg):
    sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")
    manifest = backup.create(cfg, now="20260728-000007", with_sources=False)
    assert manifest["sources_included"] is False


def test_backup_without_any_sources_still_works(cfg):
    cfg.save()
    manifest = backup.create(cfg, now="20260728-000008")
    assert manifest["sources_included"] is False
    assert backup.verify(cfg, "20260728-000008")["ok"] is True


def test_cli_backup_flags(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")
    assert main(["backup", "create", "--no-sources"]) == 0
    assert json.loads(capsys.readouterr().out)["sources_included"] is False
    assert main(["backup", "create", "--with-sources"]) == 0
    assert json.loads(capsys.readouterr().out)["sources_included"] is True
