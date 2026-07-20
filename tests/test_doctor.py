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
    monkeypatch.setattr(scheduler, "register", lambda job: "OK")
    monkeypatch.setattr(scheduler, "unregister", lambda job: "OK")
    import singularity.search.client as client_mod
    import singularity.search.model_store as model_store_mod
    monkeypatch.setattr(client_mod, "ping", lambda cfg: None)
    monkeypatch.setattr(client_mod, "start_daemon", lambda cfg: True)
    monkeypatch.setattr(client_mod, "stop_daemon", lambda cfg: True)
    monkeypatch.setattr(model_store_mod, "status",
                        lambda cfg: {"installed": True, "model": cfg.model,
                                     "snapshot": "/test/model", "revision": "test"})
    monkeypatch.setattr(doctor, "_run_shim", lambda sh, shim: (0, "mb 0.2.0", ""))


def test_b3_reports_missing_local_model(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import singularity.search.model_store as model_store_mod
    monkeypatch.setattr(model_store_mod, "status",
                        lambda cfg: {"installed": False, "model": cfg.model,
                                     "cache": str(cfg.hf_cache),
                                     "reason": "manifest missing"})
    doctor.run(cfg, as_json=True)
    check = _check(cfg, "B3-model")
    assert check["status"] == "FAIL"
    assert "doctor --fix --heavy" in check["fix"]


def test_b3_fix_heavy_installs_model_and_restarts_daemon(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import singularity.search.client as client_mod
    import singularity.search.model_store as model_store_mod
    calls = []
    monkeypatch.setattr(model_store_mod, "status",
                        lambda cfg: {"installed": False, "model": cfg.model,
                                     "cache": str(cfg.hf_cache),
                                     "reason": "manifest missing"})
    monkeypatch.setattr(model_store_mod, "install",
                        lambda cfg: calls.append("install") or
                        {"installed": True, "source": "download"})
    monkeypatch.setattr(client_mod, "stop_daemon",
                        lambda cfg: calls.append("stop") or True)
    monkeypatch.setattr(client_mod, "start_daemon",
                        lambda cfg: calls.append("start") or True)
    monkeypatch.setattr(client_mod, "run_index",
                        lambda cfg, action="update", files=None: {"chunks": 0})
    doctor.run(cfg, fix=True, heavy=True, as_json=True)
    assert calls[:3] == ["install", "stop", "start"]
    assert _check(cfg, "B3-model")["status"] == "FIXED"


def test_b4_reads_current_generation_layout(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import numpy as np
    gen = cfg.memory_index_dir / "gen_test"
    gen.mkdir(parents=True)
    np.save(gen / "embeddings.npy", np.ones((1, 3), dtype=np.float32))
    (gen / "metadata.json").write_text(
        json.dumps({"model": cfg.model, "chunks": [{"file": "x.md"}]}),
        encoding="utf-8")
    (cfg.memory_index_dir / "CURRENT").write_text("gen_test", encoding="utf-8")
    doctor.run(cfg, as_json=True)
    check = _check(cfg, "B4-index")
    assert check["status"] == "PASS"
    assert "1 chunks" in check["detail"]


def test_b4_fix_heavy_rebuilds_via_daemon(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import singularity.search.client as client_mod
    calls = []
    monkeypatch.setattr(client_mod, "run_index",
                        lambda cfg, action="update", files=None:
                        calls.append(action) or {"chunks": 2})
    doctor.run(cfg, fix=True, heavy=True, as_json=True)
    assert calls == ["rebuild"]
    assert _check(cfg, "B4-index")["status"] == "FIXED"


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


def _check(cfg, check_id):
    return next(c for c in _parse_checks(cfg) if c["id"] == check_id)


# --- T02: D1-hooks — desired/installed/effective, intentional disable != FAIL --

def test_d1_hooks_warns_when_no_client_registered(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import singularity.hooks_state as hooks_state
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: cfg.vault))
    doctor.run(cfg, as_json=True)
    d1 = _check(cfg, "D1-hooks")
    assert d1["status"] == "WARN"
    assert "no client has the hook registered" in d1["detail"]


def test_d1_hooks_pass_when_installed_and_enabled(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import singularity.hooks_state as hooks_state
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: cfg.vault))
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(
        {"hooks": {"Stop": [{"_mb": True, "hooks": [{"command": "x"}]}]}}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d1 = _check(cfg, "D1-hooks")
    assert d1["status"] == "PASS"
    assert "claude=installed" in d1["detail"]


def test_d1_hooks_pass_not_fail_when_intentionally_disabled(cfg, monkeypatch):
    """The whole point of T02: a deliberate `mb hooks disable` must never read
    as broken installation."""
    _stub_externals(monkeypatch)
    import singularity.hooks_state as hooks_state
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: cfg.vault))
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(
        {"hooks": {"Stop": [{"_mb": True, "hooks": [{"command": "x"}]}]}}), encoding="utf-8")
    (cfg.vault / ".mb").mkdir(parents=True, exist_ok=True)
    (cfg.vault / ".mb" / "config.json").write_text(
        json.dumps({"auto_capture": False}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d1 = _check(cfg, "D1-hooks")
    assert d1["status"] == "PASS"
    assert "intentionally disabled" in d1["detail"]


def test_d1_hooks_warns_on_malformed_config(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    import singularity.hooks_state as hooks_state
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: cfg.vault))
    (cfg.vault / ".mb").mkdir(parents=True, exist_ok=True)
    (cfg.vault / ".mb" / "config.json").write_text("{not json", encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d1 = _check(cfg, "D1-hooks")
    assert d1["status"] == "WARN"
    assert "unreadable" in d1["detail"]


# --- T07: D3-module-context ----------------------------------------------------

def _home(monkeypatch, cfg, home=None):
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: home or cfg.vault))
    import singularity.hooks_state as hooks_state
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: home or cfg.vault))


def test_d3_skips_when_no_claude_settings(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D3-module-context")["status"] == "SKIP"


def test_d3_warns_when_not_registered(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d3 = _check(cfg, "D3-module-context")
    assert d3["status"] == "WARN"
    assert "not registered" in d3["detail"]


def test_d3_pass_when_marked_entry_present(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"_mb": True, "matcher": "Edit|Write|MultiEdit",
         "hooks": [{"type": "command", "command": "x"}]}]}}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D3-module-context")["status"] == "PASS"


def test_d3_pass_via_command_path_match_without_marker(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command",
                    "command": '"...python.exe" ".../singularity/hooks/module_context.py"'}]}]}}),
        encoding="utf-8")
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D3-module-context")["status"] == "PASS"


def test_d3_warns_on_malformed_settings(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D3-module-context")["status"] == "WARN"


# --- T08: D5-vault-guard --------------------------------------------------------

def test_d5_skips_when_no_claude_settings(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D5-vault-guard")["status"] == "SKIP"


def test_d5_warns_when_not_registered(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d5 = _check(cfg, "D5-vault-guard")
    assert d5["status"] == "WARN"
    assert "not registered" in d5["detail"]


def test_d5_pass_when_registered_reports_disabled_by_default(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"_mb": True, "matcher": "Edit|Write|MultiEdit",
         "hooks": [{"type": "command", "command": "x vault_guard.py"}]}]}}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d5 = _check(cfg, "D5-vault-guard")
    assert d5["status"] == "PASS"
    assert "disabled" in d5["detail"]


# --- T12: C1-nightly job migration --------------------------------------------

def test_c1_fix_migrates_legacy_nightly_index_job(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    calls = []

    def fake_verify(job):
        return {"registered": job.name == "nightly-index"}
    monkeypatch.setattr(scheduler, "verify", fake_verify)
    monkeypatch.setattr(scheduler, "unregister",
                        lambda job: calls.append(("unregister", job.name)))
    monkeypatch.setattr(scheduler, "register",
                        lambda job: calls.append(("register", job.name)) or "OK")
    doctor.run(cfg, fix=True, as_json=True)
    assert ("unregister", "nightly-index") in calls
    assert ("register", "nightly") in calls
    assert _check(cfg, "C1-nightly")["status"] == "FIXED"


def test_c1_warns_without_fix_when_only_legacy_job_present(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(scheduler, "verify",
                        lambda job: {"registered": job.name == "nightly-index"})
    doctor.run(cfg, fix=False, as_json=True)
    c1 = _check(cfg, "C1-nightly")
    assert c1["status"] == "WARN"
    assert "not registered" in c1["detail"]


# --- D6: POSIX shim smoke (incident "mb shim silently dies in Git Bash") ------

def test_d6_pass_when_shim_answers(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/git/{name}.exe")
    doctor.run(cfg, as_json=True)
    d6 = _check(cfg, "D6-posix-shim")
    assert d6["status"] == "PASS"
    assert "mb 0.2.0" in d6["detail"]


def test_d6_fail_on_silent_exit_127(cfg, monkeypatch):
    """The exact incident shape: exit 127, zero bytes of output."""
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/git/{name}.exe")
    monkeypatch.setattr(doctor, "_run_shim", lambda sh, shim: (127, "", ""))
    doctor.run(cfg, as_json=True)
    d6 = _check(cfg, "D6-posix-shim")
    assert d6["status"] == "FAIL"
    assert "exit 127" in d6["detail"]
    assert "stdout empty" in d6["detail"]


def test_d6_fail_when_exit_zero_but_silent(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/git/{name}.exe")
    monkeypatch.setattr(doctor, "_run_shim", lambda sh, shim: (0, "", ""))
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D6-posix-shim")["status"] == "FAIL"


def test_d6_skip_without_posix_shell(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    doctor.run(cfg, as_json=True)
    d6 = _check(cfg, "D6-posix-shim")
    assert d6["status"] == "SKIP"
    assert "no sh/bash" in d6["detail"]


def test_d5_reports_mode_when_enabled(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"_mb": True, "hooks": [{"type": "command", "command": "x vault_guard.py"}]}]}}),
        encoding="utf-8")
    (cfg.vault / ".mb").mkdir(parents=True, exist_ok=True)
    (cfg.vault / ".mb" / "config.json").write_text(
        json.dumps({"vault_guard_enabled": True, "vault_guard_mode": "deny"}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d5 = _check(cfg, "D5-vault-guard")
    assert d5["status"] == "PASS"
    assert "mode=deny" in d5["detail"]
