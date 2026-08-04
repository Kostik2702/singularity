"""T01: `mb hooks status|enable|disable` CLI wiring, human + --json parity,
idempotency, and the malformed-config-never-overwrites guarantee."""
import json

import pytest

from singularity import cli
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return c


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


def test_status_json_has_expected_schema(wired, capsys):
    rc = cli.main(["hooks", "status", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out.strip())
    assert data["kind"] == "capture_hooks"
    assert data["configured"] is True
    assert data["source"] == "default"
    for name in ("claude", "gemini", "codex", "grok"):
        assert set(data["clients"][name]) == {"installed", "supported", "mode", "effective"}
    # GRK-04: Grok mirrors Claude's block-mode Stop gate (installed varies by
    # machine state, so only capability fields are pinned here).
    assert data["clients"]["grok"]["supported"] is True
    assert data["clients"]["grok"]["mode"] == "block"


def test_status_human_output_mentions_all_clients(wired, capsys):
    rc = cli.main(["hooks", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "capture hooks: enabled" in out
    assert "claude:" in out and "gemini:" in out and "codex:" in out and "grok:" in out


def test_disable_then_status_reflects_it(wired, capsys):
    assert cli.main(["hooks", "disable"]) == 0
    capsys.readouterr()
    cli.main(["hooks", "status", "--json"])
    data = json.loads(capsys.readouterr().out.strip())
    assert data["configured"] is False
    assert data["source"] == "explicit"


def test_enable_disable_round_trip_is_idempotent(wired, capsys):
    assert cli.main(["hooks", "disable"]) == 0
    assert cli.main(["hooks", "disable"]) == 0     # second disable: no error, stays false
    capsys.readouterr()
    cli.main(["hooks", "status", "--json"])
    assert json.loads(capsys.readouterr().out.strip())["configured"] is False

    assert cli.main(["hooks", "enable"]) == 0
    assert cli.main(["hooks", "enable"]) == 0      # idempotent
    capsys.readouterr()
    cli.main(["hooks", "status", "--json"])
    assert json.loads(capsys.readouterr().out.strip())["configured"] is True


def test_existing_config_set_auto_capture_still_works(wired, cfg):
    assert cli.main(["config", "set", "auto_capture", "false"]) == 0
    assert cfg.data["auto_capture"] is False


def test_disable_preserves_foreign_and_unknown_keys(wired, cfg, capsys):
    cfg.data["totally_unknown_key"] = "keep-me"
    cfg.data["model"] = "custom-model"
    cli.main(["hooks", "disable"])
    from singularity.config import CONFIG_FILE
    on_disk = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))   # per machine now
    assert on_disk["totally_unknown_key"] == "keep-me"
    assert on_disk["model"] == "custom-model"
    assert on_disk["auto_capture"] is False


# --- malformed config never overwritten, actionable error ----------------------

def test_malformed_config_blocks_enable_before_any_write(tmp_path, capsys):
    mb = tmp_path / ".mb"
    mb.mkdir(parents=True)
    bad = mb / "config.json"
    bad.write_text("{not json", encoding="utf-8")
    original = bad.read_text(encoding="utf-8")

    # main() reports the failure by return code now (config raises instead of
    # exiting); the console script turns that into the same exit status 2.
    assert cli.main(["--vault", str(tmp_path), "hooks", "enable"]) == 2
    assert "[mb]" in capsys.readouterr().err
    assert bad.read_text(encoding="utf-8") == original     # untouched


def test_malformed_config_blocks_status_before_any_write(tmp_path, capsys):
    mb = tmp_path / ".mb"
    mb.mkdir(parents=True)
    (mb / "config.json").write_text("{not json", encoding="utf-8")

    assert cli.main(["--vault", str(tmp_path), "hooks", "status"]) == 2
