"""Unattended launches must not inherit a CWD that shadows the package (#16).

The shims are the reported symptom, but `-m singularity` is the mechanism, and
three launch paths use it with a CWD nobody chose:

- the detached daemon spawn — inherits whoever ran `mb daemon start`;
- systemd user units — default CWD is `$HOME`, and the README says to clone
  into `~/singularity`, which makes `$HOME` a shadowing directory for every
  scheduled run;
- the cron fallback — same, cron runs from `$HOME`.

Each of those must pin `module_search_root()` instead.
"""
from pathlib import Path

import pytest

from singularity.platform import proc, scheduler


def test_module_search_root_holds_a_regular_singularity_package():
    """The whole point: a directory where `singularity/__init__.py` exists, so
    a regular package always beats any namespace portion elsewhere."""
    root = Path(proc.module_search_root())
    assert (root / "singularity" / "__init__.py").is_file()


def test_spawn_detached_defaults_to_the_safe_root(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        pid = 4242

        def __init__(self, args, **kw):
            seen.update(kw)

    monkeypatch.setattr(proc.subprocess, "Popen", FakePopen)
    proc.spawn_detached(["python", "-m", "singularity"], tmp_path / "d.log")
    assert seen["cwd"] == proc.module_search_root()


def test_spawn_detached_honours_an_explicit_cwd(monkeypatch, tmp_path):
    seen = {}

    class FakePopen:
        pid = 1

        def __init__(self, args, **kw):
            seen.update(kw)

    monkeypatch.setattr(proc.subprocess, "Popen", FakePopen)
    proc.spawn_detached(["python"], tmp_path / "d.log", cwd=str(tmp_path))
    assert seen["cwd"] == str(tmp_path)


def test_daemon_spawn_pins_the_safe_root(monkeypatch, tmp_path):
    """`mb daemon start` from ~/projects must not produce a daemon that dies on
    import and is reported only as a 30s startup timeout."""
    from singularity.config import Config
    from singularity.search import client

    cfg = Config(vault=tmp_path, data={})
    seen = {}
    monkeypatch.setattr(client, "ping", lambda cfg: None)
    monkeypatch.setattr(client.proc, "spawn_detached",
                        lambda args, log, cwd=None: seen.update(cwd=cwd) or 1)
    client.start_daemon(cfg, wait=False)
    assert seen["cwd"] == proc.module_search_root()


@pytest.mark.parametrize("job", [
    scheduler.Job("nightly-index", ["-m", "singularity", "nightly"], daily_at="03:00"),
    scheduler.Job("watch", ["-m", "singularity", "watch"], at_login=True),
])
def test_windows_task_xml_pins_a_working_directory(job):
    xml = scheduler._win_task_xml("py.exe", job.args,
                                  daily_at=job.daily_at, at_login=job.at_login)
    assert f"<WorkingDirectory>{proc.module_search_root()}</WorkingDirectory>" in xml


def test_cron_command_cds_into_the_safe_root():
    line = scheduler._cron_command("/venv/bin/python", ["-m", "singularity", "nightly"])
    assert line.startswith("cd ")
    assert proc.module_search_root() in line
    assert "&& /venv/bin/python -m singularity nightly" in line


def test_systemd_units_pin_a_working_directory(monkeypatch, tmp_path):
    """Both unit shapes — the oneshot timer service and the at-login service."""
    monkeypatch.setattr(scheduler, "_linux_has_systemd", lambda: True)
    monkeypatch.setattr(scheduler, "_systemd_dir", lambda: tmp_path)
    monkeypatch.setattr(scheduler, "python_executable", lambda windowed=False: "/venv/bin/python")
    monkeypatch.setattr(scheduler, "_run", lambda cmd: type("R", (), {"returncode": 0, "stderr": "", "stdout": ""})())

    scheduler._linux_register(scheduler.Job("nightly-index", ["-m", "singularity"],
                                            daily_at="03:00"))
    daily = (tmp_path / "singularity-nightly-index.service").read_text(encoding="utf-8")
    quoted = scheduler._systemd_quote(proc.module_search_root())
    assert f"WorkingDirectory={quoted}\n" in daily

    scheduler._linux_register(scheduler.Job("watch", ["-m", "singularity", "watch"],
                                            at_login=True))
    login = (tmp_path / "singularity-watch.service").read_text(encoding="utf-8")
    assert f"WorkingDirectory={quoted}\n" in login
