"""Vault resolution (github#1: bare Windows default leaked onto macOS)."""
import json

from singularity import config
from singularity.config import Config


def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("MB_HOME", raising=False)
    monkeypatch.setattr(config, "POINTER_FILE", tmp_path / "no-pointer.json")
    # the built-in candidates are home-relative now, so point the whole list at
    # a path that does not exist instead of stubbing one machine's constant
    monkeypatch.setenv("MB_DEFAULT_VAULT", str(tmp_path / "no-such-vault"))


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
    pointer.write_text('{{"vault_path": "{}"}}'.format(str(tmp_path).replace("\\", "\\\\")),
                       encoding="utf-8")
    monkeypatch.setattr(config, "POINTER_FILE", pointer)
    assert config.resolve_vault(None) == tmp_path


def test_default_used_only_if_it_exists(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    existing = tmp_path / "dev-vault"
    existing.mkdir()
    monkeypatch.setenv("MB_DEFAULT_VAULT", str(existing))
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
    # a legacy config with no schema_version, in its pre-0.7 vault location
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"model": "m", "min_score": 0.3}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))

    cfg = cfgmod.load_config(str(tmp_path))

    assert cfg.data["schema_version"] == cfgmod.SCHEMA_VERSION
    # The pre-migration content is preserved as the `.migrated-*` file the move
    # leaves behind — the schema bump rides along with the adoption, so a
    # second `.bak-*` beside it would back up nothing that is not already kept.
    kept = list(mb.glob("config.json.migrated-*"))
    assert kept and "min_score" in kept[0].read_text(encoding="utf-8")


# --- 0.7: the config leaves the vault, seamlessly ---------------------------

def test_vault_config_is_adopted_and_moved_aside(tmp_path, monkeypatch):
    """The user runs nothing: the next `mb` command adopts the old file."""
    from singularity import config as cfgmod
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text(
        '{"model": "m", "code_repos_dir": "X:/repos"}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))

    cfg = cfgmod.load_config(str(tmp_path))

    assert cfg.get("code_repos_dir") == "X:/repos"          # value survived
    assert cfgmod.CONFIG_FILE.exists()                      # now on the machine
    assert not (mb / "config.json").exists()                # gone from the vault
    assert list(mb.glob("config.json.migrated-*"))          # kept, not deleted


def test_migration_never_overwrites_a_configured_machine(tmp_path, monkeypatch):
    """A machine that already has settings must win over a vault it opens."""
    from singularity import config as cfgmod
    cfgmod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfgmod.CONFIG_FILE.write_text('{"model": "mine"}', encoding="utf-8")
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"model": "theirs"}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))

    cfg = cfgmod.load_config(str(tmp_path))

    assert cfg.get("model") == "mine"
    assert (mb / "config.json").exists()          # left alone entirely


def test_migration_tells_a_running_daemon_to_restart(tmp_path, monkeypatch, caplog):
    """A daemon started before the move holds the OLD module: its config path
    still resolves into the vault, so it keeps serving the file just renamed
    and `mb daemon reload` reports nothing changed. Settings then look applied
    everywhere except where they are read. Migration is the one moment we know
    this happened, so it must say so."""
    from singularity import config as cfgmod
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"model": "m"}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    monkeypatch.setattr(cfgmod, "_daemon_running", lambda cfg: True)

    with caplog.at_level("INFO", logger="mb"):
        cfgmod.load_config(str(tmp_path))

    # The notice is a log record now (a daemon or hook must be able to route
    # it somewhere other than a user's stderr); the CLI prints it verbatim.
    assert any("mb daemon restart" in r.getMessage() for r in caplog.records)


def test_migration_is_quiet_about_a_daemon_that_is_not_running(tmp_path, monkeypatch, caplog):
    from singularity import config as cfgmod
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"model": "m"}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    monkeypatch.setattr(cfgmod, "_daemon_running", lambda cfg: False)

    with caplog.at_level("INFO", logger="mb"):
        cfgmod.load_config(str(tmp_path))

    assert not any("daemon restart" in r.getMessage() for r in caplog.records)


def test_saved_config_stays_small_because_defaults_are_never_injected(tmp_path, monkeypatch):
    """The file must stay short as the schema grows — forty materialised
    defaults is what made the old one unreadable.

    That is achieved by never putting defaults INTO `data`, not by filtering
    them out on the way to disk: what a caller never set is simply not there.
    """
    from singularity import config as cfgmod
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    cfg = cfgmod.Config(vault=tmp_path, data={"model": "custom"})
    cfg.save()

    written = json.loads(cfgmod.CONFIG_FILE.read_text(encoding="utf-8"))
    assert set(written) == {"schema_version", "vault_path", "model"}
    assert len(written) < len(cfgmod.DEFAULT_CONFIG)


def test_a_value_equal_to_the_default_is_still_recorded(tmp_path, monkeypatch):
    """Choosing what the default happens to be today is a choice, and it has to
    survive the default changing tomorrow.

    Dropping such keys is what made `hybrid_search` impossible to pin: while the
    default was `false`, `mb config set hybrid_search false` deleted the key, so
    when the default flipped every install flipped with it and none could have
    opted out. The same hole voided the v0.6.3 promise about
    `vault_guard_enabled` — see `decisions/SEARCH D1 - гибрид по умолчанию.md`.
    """
    from singularity import config as cfgmod
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    pinned = cfgmod.DEFAULT_CONFIG["min_score"]
    cfg = cfgmod.Config(vault=tmp_path, data={"min_score": pinned})
    cfg.save()

    written = json.loads(cfgmod.CONFIG_FILE.read_text(encoding="utf-8"))
    assert written["min_score"] == pinned, "an explicitly held value was dropped"


def test_a_pin_survives_the_default_moving_underneath_it(tmp_path, monkeypatch):
    """The property the whole change exists for, proven by moving the default.

    A test that only checks the key is written would pass even if `get()` still
    preferred the new default. This one reloads and reads the effective value
    after `DEFAULT_CONFIG` has changed under the saved file.
    """
    from singularity import config as cfgmod
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    old_default = cfgmod.DEFAULT_CONFIG["hybrid_search"]
    cfg = cfgmod.Config(vault=tmp_path, data={"hybrid_search": old_default})
    cfg.save()

    monkeypatch.setitem(cfgmod.DEFAULT_CONFIG, "hybrid_search", not old_default)
    reloaded = cfgmod.Config(
        vault=tmp_path,
        data=json.loads(cfgmod.CONFIG_FILE.read_text(encoding="utf-8")))
    assert reloaded.get("hybrid_search") is old_default, (
        "the machine followed the new default instead of the value it pinned")


def test_write_pointer_keeps_the_other_settings(tmp_path, monkeypatch):
    """It used to rewrite the whole file with one key — fine for a pointer,
    destructive for a config."""
    from singularity import config as cfgmod
    cfgmod.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfgmod.CONFIG_FILE.write_text('{"model": "keep-me", "vault_path": "/old"}',
                                  encoding="utf-8")

    cfgmod.write_pointer(tmp_path)

    data = json.loads(cfgmod.CONFIG_FILE.read_text(encoding="utf-8"))
    assert data["vault_path"] == str(tmp_path)
    assert data["model"] == "keep-me"


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


def test_load_config_rejects_bad_types_actionably(tmp_path, monkeypatch):
    """Raises instead of exiting (a daemon must survive a bad file), and the
    diagnostic it carries is the one the CLI prints verbatim."""
    from singularity import config as cfgmod
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"daemon_port": 99999}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    import pytest
    with pytest.raises(cfgmod.ConfigError) as ei:
        cfgmod.load_config(str(tmp_path))
    assert ei.value.code == 2
    assert any(ln.startswith("[mb]") for ln in ei.value.lines)   # actionable, not a traceback


def test_cli_still_reports_a_bad_config_the_same_way(tmp_path, monkeypatch, capsys):
    """The exception must not change what a user sees: same lines, same code 2."""
    from singularity import cli
    mb = tmp_path / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text('{"daemon_port": 99999}', encoding="utf-8")
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    assert cli.main(["--vault", str(tmp_path), "status"]) == 2
    assert "[mb]" in capsys.readouterr().err
