"""AUD-02: doctor must verify a WORKING path, not mere presence.

- `--json` emits exactly one valid JSON document, no human text interleaved.
- scheduler check compares the stored action against expected argv (a task
  registered with a broken --vault must not read as PASS).
- watcher freshness is checked via a heartbeat file.
"""
import json
from pathlib import Path

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


# --- C2-watcher: login-task argv verify + re-register fix ---------------------
# Incident 2026-07-20: the watch task registered by a pre-AUD-01 installer kept
# an unquoted vault path in its stored action and exited 1 at every login; C2
# only saw "no heartbeat" and the broken registration read as background noise.

def test_watch_job_is_single_source(cfg):
    job = watch.watch_job(cfg)
    assert job.at_login and job.daily_at is None
    assert job.args == ["-m", "singularity", "--vault", str(cfg.vault), "watch"]
    assert job.task_id == "Singularity-watch"


def test_c2_fails_without_fix_on_argv_mismatch(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")

    def fake_verify(job):
        if job.name == "watch":
            return {"registered": True,
                    "arguments": "-m singularity --vault D:\\a b watch"}
        return {"registered": False}
    monkeypatch.setattr(scheduler, "verify", fake_verify)
    doctor.run(cfg, as_json=True)
    c2 = _check(cfg, "C2-watcher")
    assert c2["status"] == "FAIL"
    assert "expected argv" in c2["detail"]


def test_c2_fix_reregisters_on_argv_mismatch(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")
    calls = []

    def fake_verify(job):
        if job.name == "watch":
            return {"registered": True, "arguments": "broken action"}
        return {"registered": False}
    monkeypatch.setattr(scheduler, "verify", fake_verify)
    monkeypatch.setattr(scheduler, "register",
                        lambda job: calls.append(job.name) or "OK")
    doctor.run(cfg, fix=True, as_json=True)
    assert "watch" in calls
    assert _check(cfg, "C2-watcher")["status"] == "FIXED"


def test_c2_fix_registers_when_task_missing(cfg, monkeypatch):
    _stub_externals(monkeypatch)          # verify -> registered False for all jobs
    calls = []
    monkeypatch.setattr(scheduler, "register",
                        lambda job: calls.append(job.name) or "OK")
    doctor.run(cfg, fix=True, as_json=True)
    assert "watch" in calls
    assert _check(cfg, "C2-watcher")["status"] == "FIXED"


def test_c2_heartbeat_checked_once_registration_healthy(cfg, monkeypatch):
    _stub_externals(monkeypatch)

    def fake_verify(job):
        if job.name == "watch":
            return {"registered": True}   # non-windows shape: no arguments read-back
        return {"registered": False}
    monkeypatch.setattr(scheduler, "verify", fake_verify)
    watch._write_heartbeat(cfg)
    doctor.run(cfg, as_json=True)
    c2 = _check(cfg, "C2-watcher")
    assert c2["status"] == "PASS"
    assert "heartbeat" in c2["detail"]


# --- --json contract ---------------------------------------------------------

def _stub_externals(monkeypatch, tmp_path=None):
    """Neutralise heavy/external checks so a doctor run is fast + deterministic.

    Also isolates the client blocks (E-gemini / F-grok): a doctor.run(fix=True)
    against the developer's real home would otherwise run the REAL registrars
    and mutate ~/.gemini / ~/.grok — exactly what happened once with the grok
    block (2026-07-23) before this isolation existed."""
    import pathlib
    import shutil as shutil_mod
    import tempfile
    fake_home = (tmp_path / "home") if tmp_path is not None else \
        pathlib.Path(tempfile.mkdtemp(prefix="doctor-fake-home-"))
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("GROK_HOME", raising=False)
    monkeypatch.setattr(shutil_mod, "which", lambda _n: None)
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
    monkeypatch.setattr(doctor, "_run_shim",
                        lambda sh, shim, cwd=None: (0, "mb 0.2.0", ""))


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
    monkeypatch.setattr(doctor, "_run_shim",
                        lambda sh, shim, cwd=None: (127, "", ""))
    doctor.run(cfg, as_json=True)
    d6 = _check(cfg, "D6-posix-shim")
    assert d6["status"] == "FAIL"
    assert "exit 127" in d6["detail"]
    assert "stdout empty" in d6["detail"]


def test_d6_fail_when_exit_zero_but_silent(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/git/{name}.exe")
    monkeypatch.setattr(doctor, "_run_shim",
                        lambda sh, shim, cwd=None: (0, "", ""))
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D6-posix-shim")["status"] == "FAIL"


# --- D6 must not depend on where doctor was launched from (issue #16) --------

def test_d6_probes_a_neutral_cwd_and_a_shadowing_cwd(cfg, monkeypatch):
    """The namespace-shadowing failure only shows from a CWD that contains a
    `singularity/` directory, so the check has to stand in both places itself
    instead of inheriting doctor's."""
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/git/{name}.exe")
    # sampled inside the call: the temp tree is gone once doctor returns
    seen = []
    monkeypatch.setattr(
        doctor, "_run_shim",
        lambda sh, shim, cwd=None:
            seen.append((cwd, (Path(cwd) / "singularity").is_dir() if cwd else None))
            or (0, "mb 0.2.0", ""))
    doctor.run(cfg, as_json=True)

    assert _check(cfg, "D6-posix-shim")["status"] == "PASS"
    assert len(seen) == 2
    assert [shadows for _cwd, shadows in seen] == [False, True]
    assert len({cwd for cwd, _ in seen}) == 2                # two distinct dirs


def test_d6_fails_when_the_shim_only_dies_in_a_shadowing_cwd(cfg, monkeypatch):
    """The regression of issue #16 verbatim: fine from anywhere else, dead one
    level above the clone. A check that passed here would be the bug."""
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"C:/git/{name}.exe")

    def fake(sh, shim, cwd=None):
        if (Path(cwd) / "singularity").is_dir():
            return 1, "", ("ImportError: cannot import name '__version__' "
                           "from 'singularity' (unknown location)")
        return 0, "mb 0.2.0", ""

    monkeypatch.setattr(doctor, "_run_shim", fake)
    doctor.run(cfg, as_json=True)

    d6 = _check(cfg, "D6-posix-shim")
    assert d6["status"] == "FAIL"
    assert "containing singularity/" in d6["detail"]
    assert "unknown location" in d6["detail"]
    assert "python -m" in d6["fix"]


def test_d6_skip_without_posix_shell(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)
    doctor.run(cfg, as_json=True)
    d6 = _check(cfg, "D6-posix-shim")
    assert d6["status"] == "SKIP"
    assert "no sh/bash" in d6["detail"]


# --- D8: can git actually commit into the vault? (issue #11) -----------------

def _git_stub(monkeypatch, *, inside=True, name="", email="", git="C:/git/git.exe"):
    """Stand in for the two read-only git calls D8 makes."""
    monkeypatch.setattr(doctor.shutil, "which",
                        lambda n: git if n == "git" else f"C:/git/{n}.exe")

    class R:
        def __init__(self, out):
            self.stdout, self.returncode = out, 0

    def fake_run(cmd, **_kw):
        if "rev-parse" in cmd:
            return R("true\n" if inside else "false\n")
        if "user.name" in cmd:
            return R(name)
        if "user.email" in cmd:
            return R(email)
        return R("")

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)


def test_d8_warns_when_commit_identity_is_unset(cfg, monkeypatch):
    """The default state of a new machine — and the state that made an install
    reporting PASS 28 still fail at the first vault commit."""
    _stub_externals(monkeypatch)
    _git_stub(monkeypatch, inside=True, name="", email="")
    doctor.run(cfg, as_json=True)
    d8 = _check(cfg, "D8-git-identity")
    assert d8["status"] == "WARN"
    assert "user.name" in d8["detail"] and "user.email" in d8["detail"]
    assert "git config --global" in d8["fix"]


def test_d8_passes_when_identity_is_configured(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _git_stub(monkeypatch, name="Ada\n", email="ada@example.com\n")
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D8-git-identity")["status"] == "PASS"


def test_d8_reports_only_the_missing_half(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _git_stub(monkeypatch, name="Ada\n", email="")
    doctor.run(cfg, as_json=True)
    d8 = _check(cfg, "D8-git-identity")
    assert d8["status"] == "WARN"
    assert "user.email" in d8["detail"] and "user.name" not in d8["detail"]


def test_d8_skips_when_the_vault_is_not_a_git_repo(cfg, monkeypatch):
    """A vault that is not under git needs no commit identity — warning about
    it would be noise, not diagnosis."""
    _stub_externals(monkeypatch)
    _git_stub(monkeypatch, inside=False)
    doctor.run(cfg, as_json=True)
    assert _check(cfg, "D8-git-identity")["status"] == "SKIP"


def test_d8_never_auto_fixes_the_identity(cfg, monkeypatch):
    """A commit identity is a personal choice, permanently embedded in
    published history. `--fix` must warn, never guess."""
    _stub_externals(monkeypatch)
    writes = []

    _git_stub(monkeypatch, name="", email="")
    real_run = doctor.subprocess.run

    def watching_run(cmd, **kw):
        if "config" in cmd and "--get" not in cmd:
            writes.append(cmd)
        return real_run(cmd, **kw)

    monkeypatch.setattr(doctor.subprocess, "run", watching_run)
    doctor.run(cfg, fix=True, as_json=True)

    assert writes == []
    assert _check(cfg, "D8-git-identity")["status"] == "WARN"


# --- D7: is `mb` actually callable by name? (issue #15) ----------------------

def _stub_register_path(monkeypatch, st):
    """doctor loads install/register_path.py by path, so the stub has to be
    installed on the object doctor gets back from _load_install."""
    real_load = doctor._load_install

    def fake_load(name):
        mod = real_load(name)
        if name == "register_path":
            mod.status = lambda: st
        return mod

    monkeypatch.setattr(doctor, "_load_install", fake_load)


def test_d7_pass_when_mb_resolves_into_this_engine(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _stub_register_path(monkeypatch, {
        "engine": "/engine", "venv_bin": "/engine/.venv/bin",
        "resolved": "/engine/.venv/bin/mb", "on_path": True, "is_ours": True})
    doctor.run(cfg, as_json=True)
    d7 = _check(cfg, "D7-cli-on-path")
    assert d7["status"] == "PASS"
    assert "/engine/.venv/bin/mb" in d7["detail"]


def test_d7_warns_when_mb_is_not_on_path(cfg, monkeypatch):
    """The state issue #15 reported: doctor clean, `mb doctor` itself
    `command not found`."""
    _stub_externals(monkeypatch)
    _stub_register_path(monkeypatch, {
        "engine": "/engine", "venv_bin": "/engine/.venv/bin",
        "resolved": None, "on_path": False, "is_ours": False})
    doctor.run(cfg, as_json=True)
    d7 = _check(cfg, "D7-cli-on-path")
    assert d7["status"] == "WARN"
    assert "not callable by name" in d7["detail"]
    assert "register_path" in d7["fix"]


def test_d7_warns_when_another_clone_shadows_this_engine(cfg, monkeypatch):
    """Two engines against one vault is quieter and worse than no mb at all."""
    _stub_externals(monkeypatch)
    _stub_register_path(monkeypatch, {
        "engine": "/engine", "venv_bin": "/engine/.venv/bin",
        "resolved": "/old-clone/.venv/bin/mb", "on_path": True, "is_ours": False})
    doctor.run(cfg, as_json=True)
    d7 = _check(cfg, "D7-cli-on-path")
    assert d7["status"] == "WARN"
    assert "/old-clone/.venv/bin/mb" in d7["detail"]
    assert "shadowing" in d7["detail"]


def test_d5_reports_mode_when_enabled(cfg, monkeypatch):
    _stub_externals(monkeypatch)
    _home(monkeypatch, cfg)
    settings = cfg.vault / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"_mb": True, "hooks": [{"type": "command", "command": "x vault_guard.py"}]}]}}),
        encoding="utf-8")
    from singularity.config import CONFIG_FILE
    CONFIG_FILE.write_text(                                    # per machine now
        json.dumps({"vault_guard_enabled": True, "vault_guard_mode": "deny"}), encoding="utf-8")
    doctor.run(cfg, as_json=True)
    d5 = _check(cfg, "D5-vault-guard")
    assert d5["status"] == "PASS"
    assert "mode=deny" in d5["detail"]
