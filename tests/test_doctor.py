"""AUD-02: doctor must verify a WORKING path, not mere presence.

- `--json` emits exactly one valid JSON document, no human text interleaved.
- scheduler check compares the stored action against expected argv (a task
  registered with a broken --vault must not read as PASS).
- watcher freshness is checked via a heartbeat file.
"""
import json

import pytest

from singularity import doctor, watch
from singularity.config import Config
from singularity.platform import scheduler


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


# --- scheduler action comparison --------------------------------------------

def test_expected_arguments_windows(monkeypatch):
    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")
    job = scheduler.Job("nightly-index",
                        ["-m", "singularity", "--vault", r"D:\a b", "index", "update"],
                        daily_at="03:00")
    assert scheduler.expected_arguments(job) == \
        '-m singularity --vault "D:\\a b" index update'


def test_expected_arguments_non_windows_is_none(monkeypatch):
    monkeypatch.setattr(scheduler, "current_os", lambda: "linux")
    job = scheduler.Job("x", ["a"], daily_at="03:00")
    assert scheduler.expected_arguments(job) is None


def test_action_matches_true_when_equal():
    assert doctor._action_matches({"arguments": "-m singularity --vault \"D:\\a b\""},
                                  "-m singularity --vault \"D:\\a b\"")


def test_action_matches_false_when_vault_split():
    # the pre-AUD-01 breakage: stored action lost the quotes around the path
    assert not doctor._action_matches(
        {"arguments": "-m singularity --vault D:\\a b"},
        '-m singularity --vault "D:\\a b"')


def test_action_matches_true_when_uncomparable():
    # non-windows: no arguments read-back -> cannot compare, don't false-alarm
    assert doctor._action_matches({"registered": True}, None)


# --- watcher heartbeat -------------------------------------------------------

def test_watch_heartbeat_written(cfg):
    watch._write_heartbeat(cfg)
    hb = cfg.watch_heartbeat_file          # machine-local path (AUD-19)
    assert hb.exists()
    assert float(hb.read_text(encoding="utf-8")) > 0


def test_doctor_watcher_check_warns_when_no_heartbeat(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    doctor.run(cfg, as_json=True)
    c2 = [c for c in _parse_checks(cfg) if c["id"].startswith("C2")]
    assert c2, "watcher check missing"
    assert c2[0]["status"] in {"WARN", "FAIL"}


# --- --json contract ---------------------------------------------------------

def _stub_externals(monkeypatch):
    """Neutralise heavy/external checks so a doctor run is fast + deterministic."""
    monkeypatch.setattr(doctor.importlib, "import_module", lambda name: object())
    monkeypatch.setattr(scheduler, "verify", lambda job: {"registered": False})
    import singularity.search.client as client_mod
    monkeypatch.setattr(client_mod, "ping", lambda cfg: None)


def _run_capture(cfg, capsys, **kw):
    doctor.run(cfg, **kw)
    return capsys.readouterr().out


def test_json_output_is_single_valid_json(cfg, monkeypatch, capsys):
    _stub_externals(monkeypatch)
    out = _run_capture(cfg, capsys, as_json=True)
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected only JSON, got {len(lines)} lines: {lines[:3]}"
    payload = json.loads(lines[0])
    assert "counts" in payload and "checks" in payload and "ok" in payload


def test_human_output_prints_lines(cfg, monkeypatch, capsys):
    _stub_externals(monkeypatch)
    out = _run_capture(cfg, capsys, as_json=False)
    assert "mb doctor summary" in out
    assert any(line.startswith("[") for line in out.splitlines())


def test_quiet_report_does_not_print(capsys):
    rep = doctor.Report(quiet=True)
    rep.add("X1", "PASS", "detail")
    assert capsys.readouterr().out == ""
    rep2 = doctor.Report()
    rep2.add("X1", "PASS", "detail")
    assert "X1" in capsys.readouterr().out


# helpers reading the persisted report ---------------------------------------

def _parse_checks(cfg):
    data = json.loads((cfg.mb_dir / "doctor-last.json").read_text(encoding="utf-8"))
    return data["checks"]
