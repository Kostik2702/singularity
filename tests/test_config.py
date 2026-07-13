"""Vault resolution (github#1: bare Windows default leaked onto macOS)."""
import json
from pathlib import Path

from singularity import config
from singularity.config import Config


def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("MB_HOME", raising=False)
    monkeypatch.setattr(config, "POINTER_FILE", tmp_path / "no-pointer.json")
    monkeypatch.setattr(config, "DEFAULT_VAULT_WINDOWS", str(tmp_path / "no-such-vault"))


def test_nothing_configured_returns_none(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert config.resolve_vault(None) is None


def test_explicit_wins(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    assert config.resolve_vault(str(tmp_path)) == tmp_path


def test_env_mb_home(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    assert config.resolve_vault(None) == tmp_path


def test_pointer_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    pointer = tmp_path / "pointer.json"
    pointer.write_text('{"vault_path": "%s"}' % str(tmp_path).replace("\\", "\\\\"),
                       encoding="utf-8")
    monkeypatch.setattr(config, "POINTER_FILE", pointer)
    assert config.resolve_vault(None) == tmp_path


def test_default_used_only_if_it_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    existing = tmp_path / "dev-vault"
    existing.mkdir()
    monkeypatch.setattr(config, "DEFAULT_VAULT_WINDOWS", str(existing))
    assert config.resolve_vault(None) == existing


def test_save_is_atomic_no_tmp_left(tmp_path):
    cfg = Config(vault=tmp_path, data={"llm": {"server_port": 1}})
    cfg.save()
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["llm"]["server_port"] == 1
    leftovers = [p for p in cfg.config_path.parent.iterdir()
                 if p.name.startswith("config") and p.suffix != ".json"]
    assert leftovers == []


# --- T08: sanitized config.example.json --------------------------------------

def test_save_generates_example(tmp_path):
    cfg = Config(vault=tmp_path, data={"model": "m"})
    cfg.save()
    assert cfg.config_example_path.exists()


def test_example_redacts_hf_cache(tmp_path):
    cfg = Config(vault=tmp_path, data={"hf_cache": str(tmp_path / "secret-cache")})
    cfg.save()
    example = json.loads(cfg.config_example_path.read_text(encoding="utf-8"))
    assert "hf_cache" not in example


def test_example_no_leftover_tmp_file(tmp_path):
    cfg = Config(vault=tmp_path, data={"model": "m"})
    cfg.save()
    leftovers = [p for p in cfg.config_path.parent.iterdir()
                if p.name.startswith("config") and p.suffix != ".json"]
    assert leftovers == []


def test_example_home_relativizes_project_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: home))
    proj = home / "code" / "myproj"
    cfg = Config(vault=tmp_path, data={"projects": {"p": {"path": str(proj)}}})
    cfg.save()
    example = json.loads(cfg.config_example_path.read_text(encoding="utf-8"))
    assert example["projects"]["p"]["path"] == "~/code/myproj"


def test_example_retains_paths_outside_home(tmp_path, monkeypatch):
    home = tmp_path / "home"; home.mkdir()
    monkeypatch.setattr(config.Path, "home", classmethod(lambda cls: home))
    outside = tmp_path / "elsewhere" / "otherproj"
    cfg = Config(vault=tmp_path, data={"projects": {"p": {"path": str(outside)}}})
    cfg.save()
    example = json.loads(cfg.config_example_path.read_text(encoding="utf-8"))
    assert example["projects"]["p"]["path"] == str(outside)


def test_example_bootstrap_save_does_not_clobber_richer_existing(tmp_path):
    cfg = Config(vault=tmp_path, data={"projects": {"real": {"path": "D:/real"}}})
    cfg.save()
    richer = json.loads(cfg.config_example_path.read_text(encoding="utf-8"))
    assert richer["projects"]

    bootstrap_cfg = Config(vault=tmp_path, data={})    # no projects at all
    bootstrap_cfg.save()
    after = json.loads(cfg.config_example_path.read_text(encoding="utf-8"))
    assert after["projects"] == richer["projects"]     # untouched, not clobbered


def test_example_generation_failure_does_not_break_real_save(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={"model": "m"})
    monkeypatch.setattr(cfg, "_write_sanitized_example",
                        lambda merged: (_ for _ in ()).throw(RuntimeError("boom")))
    cfg.save()      # must not raise
    assert json.loads(cfg.config_path.read_text(encoding="utf-8"))["model"] == "m"


# --- AUD-13: config schema validation + migration ---------------------------

def test_validate_clean_config_has_no_errors():
    from singularity.config import validate_config
    res = validate_config({"model": "m", "min_score": 0.3, "daemon_port": 0,
                           "batch_size": 64, "skip_dirs": ["a"]})
    assert res["errors"] == []


def test_validate_flags_type_and_range_errors():
    from singularity.config import validate_config
    res = validate_config({"model": "", "min_score": 5.0, "daemon_port": 99999,
                           "batch_size": -1, "skip_dirs": "notalist",
                           "auto_capture": "yes"})
    joined = " ".join(res["errors"])
    assert "model" in joined
    assert "min_score" in joined
    assert "daemon_port" in joined
    assert "batch_size" in joined
    assert "skip_dirs" in joined
    assert "auto_capture" in joined


def test_validate_bool_is_not_accepted_as_number():
    from singularity.config import validate_config
    # True must not slip through min_score's numeric check
    assert any("min_score" in e for e in validate_config({"min_score": True})["errors"])


def test_validate_unknown_key_is_a_warning_not_error():
    from singularity.config import validate_config
    res = validate_config({"model": "m", "totally_unknown": 1})
    assert res["errors"] == []
    assert any("totally_unknown" in w for w in res["warnings"])


def test_migrate_adds_schema_version():
    from singularity.config import migrate_config, SCHEMA_VERSION
    data, changed = migrate_config({"model": "m"})
    assert changed is True
    assert data["schema_version"] == SCHEMA_VERSION


def test_migrate_is_noop_when_current():
    from singularity.config import migrate_config, SCHEMA_VERSION
    data, changed = migrate_config({"model": "m", "schema_version": SCHEMA_VERSION})
    assert changed is False


def test_load_config_migrates_and_backs_up(tmp_path, monkeypatch):
    from singularity import config as cfgmod
    # a legacy config with no schema_version
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"model": "m", "min_score": 0.3}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    cfg = cfgmod.load_config(str(tmp_path))
    assert cfg.data["schema_version"] == cfgmod.SCHEMA_VERSION
    # a backup of the pre-migration file was written
    assert list(mb.glob("config.json.bak-*"))


# --- T03: projects registry validation is merged into validate_config -------

def test_validate_config_merges_projects_errors():
    from singularity.config import validate_config
    res = validate_config({"model": "m", "projects": {"general": {"path": "D:/x"}}})
    assert any("reserved" in e for e in res["errors"])


def test_validate_config_does_not_warn_projects_as_unknown_key():
    from singularity.config import validate_config
    res = validate_config({"model": "m", "projects": {"p": {"path": "D:/x"}}})
    assert res["errors"] == []
    assert not any("projects" in w for w in res["warnings"])


# --- T12: nightly orchestration config keys ----------------------------------

def test_validate_nightly_numeric_keys_range():
    from singularity.config import validate_config
    res = validate_config({"nightly_nag_rate_limit_hours": 0, "markers_retention_days": 0,
                           "nightly_trend_retention_days": 0})
    joined = " ".join(res["errors"])
    assert "nightly_nag_rate_limit_hours" in joined
    assert "markers_retention_days" in joined
    assert "nightly_trend_retention_days" in joined


def test_validate_nightly_schedule_time_format():
    from singularity.config import validate_config
    assert any("nightly_schedule_time" in e
              for e in validate_config({"nightly_schedule_time": "3:00"})["errors"])
    assert any("nightly_schedule_time" in e
              for e in validate_config({"nightly_schedule_time": "25:00"})["errors"])
    assert validate_config({"nightly_schedule_time": "03:00"})["errors"] == []


def test_load_config_rejects_bad_types_actionably(tmp_path, monkeypatch, capsys):
    from singularity import config as cfgmod
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"daemon_port": 99999}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    import pytest
    with pytest.raises(SystemExit) as ei:
        cfgmod.load_config(str(tmp_path))
    assert ei.value.code == 2
    assert "[mb]" in capsys.readouterr().err   # actionable, not a traceback
