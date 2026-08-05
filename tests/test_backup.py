"""AUD-20: mb backup create/verify/list/restore + retention."""
import json

import pytest

from singularity.machine import backup
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    # min_score is deliberately NOT the default: from 0.7 `save()` records
    # decisions only, so a default-valued key never reaches the file and could
    # not be asserted on after a restore.
    c = Config(vault=tmp_path, data={"model": "m", "min_score": 0.42})
    c.save()                       # writes the machine config
    return c


def test_create_then_verify_ok(cfg):
    mf = backup.create(cfg, now="B1")
    assert mf["files"]["config.json"]
    assert backup.verify(cfg, "B1")["ok"] is True


def test_verify_detects_tampering(cfg):
    backup.create(cfg, now="B1")
    bad = cfg.mb_dir / "backups" / "B1" / "config.json"
    bad.write_text("TAMPERED", encoding="utf-8")
    res = backup.verify(cfg, "B1")
    assert res["ok"] is False
    assert "config.json" in res["corrupt"]


def test_verify_missing_backup(cfg):
    assert backup.verify(cfg, "nope")["ok"] is False


def test_list_backups(cfg):
    backup.create(cfg, now="B1")
    backup.create(cfg, now="B2")
    ids = [b["id"] for b in backup.list_backups(cfg)]
    assert ids == ["B1", "B2"]


def test_restore_brings_back_config(cfg):
    backup.create(cfg, now="B1")
    # mutate the live config after the backup
    cfg.data["min_score"] = 0.99
    cfg.save()
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["min_score"] == 0.99
    res = backup.restore(cfg, "B1")
    assert res["ok"] and "config.json" in res["restored"]
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["min_score"] == 0.42


def test_restore_refuses_corrupt(cfg):
    backup.create(cfg, now="B1")
    (cfg.mb_dir / "backups" / "B1" / "config.json").write_text("bad", encoding="utf-8")
    assert backup.restore(cfg, "B1")["ok"] is False


def test_restore_rolls_index_generation(cfg):
    gen = "gen_0001"
    (cfg.memory_index_dir / gen).mkdir(parents=True)
    (cfg.memory_index_dir / "CURRENT").write_text(gen, encoding="utf-8")
    backup.create(cfg, now="B1")
    (cfg.memory_index_dir / "CURRENT").write_text("gen_9999", encoding="utf-8")
    res = backup.restore(cfg, "B1")
    assert res["index_rolled_back"] is True
    assert (cfg.memory_index_dir / "CURRENT").read_text(encoding="utf-8") == gen


def test_retention_prunes_but_keeps_last(cfg):
    for i in range(13):
        backup.create(cfg, now=f"B{i:02d}")
    remaining = [b["id"] for b in backup.list_backups(cfg)]
    assert len(remaining) == backup.RETENTION      # pruned to RETENTION
    assert "B12" in remaining                       # newest kept


def test_prune_never_deletes_the_only_backup(cfg, monkeypatch):
    monkeypatch.setattr(backup, "RETENTION", 0)     # pathological keep=0
    backup.create(cfg, now="B1")
    backup._prune(cfg, keep=0)
    assert len(backup.list_backups(cfg)) == 1        # last one survives
