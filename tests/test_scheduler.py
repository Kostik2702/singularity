"""AUD-01: scheduler must preserve exact argv (spaces/Unicode/shell-special)
across Windows/macOS/Linux, and verify() must read back the real stored action.

Regression trigger: a vault path with a space (`D:\\obsidian\\memory brain`)
was joined with `" ".join(args)` and split apart by the OS scheduler ->
LastTaskResult=1. Tests exercise the pure per-platform serializers plus the
register/verify integration with a faked `_run`.
"""
import subprocess

import pytest

from singularity.platform import scheduler

VAULT = r"D:\obsidian\memory brain"          # the space that broke everything
UNICODE = r"C:\Пользователи\хранилище данных"  # non-ASCII path
ARGS = ["-m", "singularity", "--vault", VAULT, "index", "update"]


# --- Windows Arguments quoting (CommandLineToArgvW round-trip) ---------------

def test_win_arguments_quotes_path_with_space():
    s = scheduler._win_arguments(ARGS)
    assert '"D:\\obsidian\\memory brain"' in s
    assert s.split(" ")[:3] == ["-m", "singularity", "--vault"]


def test_win_task_xml_daily_has_command_arguments_and_start_when_available():
    xml = scheduler._win_task_xml(r"C:\py\pythonw.exe", ARGS, daily_at="03:00")
    assert "<Command>C:\\py\\pythonw.exe</Command>" in xml
    # quotes are literal in XML element text (valid, no escaping required)
    assert '"D:\\obsidian\\memory brain"' in xml   # arg stays one token
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml
    assert "<CalendarTrigger>" in xml
    assert "03:00:00" in xml


def test_win_task_xml_logon_trigger():
    xml = scheduler._win_task_xml("py", ["watch"], at_login=True)
    assert "<LogonTrigger>" in xml
    assert "<CalendarTrigger>" not in xml


def test_win_task_xml_requires_a_trigger():
    with pytest.raises(ValueError):
        scheduler._win_task_xml("py", ["x"])


def test_win_task_xml_preserves_unicode():
    xml = scheduler._win_task_xml("py", ["--vault", UNICODE], at_login=True)
    assert UNICODE in xml


def test_win_task_xml_escapes_xml_specials():
    xml = scheduler._win_task_xml("py", ["--q", "a & b <x>"], at_login=True)
    assert "&amp;" in xml and "&lt;x&gt;" in xml
    assert "<x>" not in xml.split("<Arguments>")[1].split("</Arguments>")[0]


# --- systemd ExecStart quoting ----------------------------------------------

def test_systemd_quote_space_backslash_percent():
    assert scheduler._systemd_quote(r"D:\a b") == '"D:\\\\a b"'
    assert scheduler._systemd_quote("50%") == '"50%%"'


def test_systemd_execstart_preserves_vault_arg():
    line = scheduler._systemd_execstart("/usr/bin/py", ARGS)
    # the vault path is a single double-quoted, backslash-escaped token
    assert '"D:\\\\obsidian\\\\memory brain"' in line
    assert line.startswith('"/usr/bin/py"')


# --- cron shell quoting ------------------------------------------------------

def test_cron_command_shell_quotes_space():
    line = scheduler._cron_command("/usr/bin/py", ["--vault", "/home/my vault"])
    assert "'/home/my vault'" in line


# --- register/verify integration (faked _run) -------------------------------

def _fake_ok(cmd):
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_win_register_writes_xml_with_quoted_vault(monkeypatch):
    captured = {}

    def fake_run(cmd):
        if "/Create" in cmd and "/XML" in cmd:
            xml_path = cmd[cmd.index("/XML") + 1]
            with open(xml_path, encoding="utf-16") as f:
                captured["xml"] = f.read()
            captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")
    monkeypatch.setattr(scheduler, "python_executable",
                        lambda windowed=False: r"C:\py\pythonw.exe")
    monkeypatch.setattr(scheduler, "_run", fake_run)

    job = scheduler.Job("nightly-index", ARGS, daily_at="03:00")
    assert scheduler.register(job) == "OK"
    assert '"D:\\obsidian\\memory brain"' in captured["xml"]
    assert captured["cmd"][:3] == ["schtasks", "/Create", "/F"]
    # temp xml file is cleaned up (path no longer exists)
    import os
    xml_path = captured["cmd"][captured["cmd"].index("/XML") + 1]
    assert not os.path.exists(xml_path)


def test_win_register_reports_failure(monkeypatch):
    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")
    monkeypatch.setattr(scheduler, "python_executable", lambda windowed=False: "py")
    monkeypatch.setattr(scheduler, "_run",
                        lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "access denied"))
    res = scheduler.register(scheduler.Job("nightly-index", ARGS, daily_at="03:00"))
    assert res.startswith("FAIL")


def test_win_verify_reads_back_argv(monkeypatch):
    stored = (
        "<Task><Actions><Exec>"
        "<Command>C:\\py\\pythonw.exe</Command>"
        "<Arguments>-m singularity --vault &quot;D:\\obsidian\\memory brain&quot; "
        "index update</Arguments>"
        "</Exec></Actions>"
        "<Settings><StartWhenAvailable>true</StartWhenAvailable></Settings></Task>"
    )
    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")
    monkeypatch.setattr(scheduler, "_run",
                        lambda cmd: subprocess.CompletedProcess(cmd, 0, stored, ""))
    st = scheduler.verify(scheduler.Job("nightly-index", ARGS, daily_at="03:00"))
    assert st["registered"] is True
    assert st["coalescing"] is True
    assert st["command"] == r"C:\py\pythonw.exe"
    assert '"D:\\obsidian\\memory brain"' in st["arguments"]   # un-escaped back


def test_win_verify_unregistered(monkeypatch):
    monkeypatch.setattr(scheduler, "current_os", lambda: "windows")
    monkeypatch.setattr(scheduler, "_run",
                        lambda cmd: subprocess.CompletedProcess(cmd, 1, "", "not found"))
    st = scheduler.verify(scheduler.Job("nightly-index", ARGS, daily_at="03:00"))
    assert st == {"registered": False}


def test_mac_register_preserves_argv_as_list(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler.Path, "home",
                        classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(scheduler, "python_executable",
                        lambda windowed=False: "/usr/bin/py")
    monkeypatch.setattr(scheduler, "_run", _fake_ok)

    job = scheduler.Job("nightly-index", ARGS, daily_at="03:00")
    scheduler._mac_register(job)

    import plistlib
    plist_path = tmp_path / "Library" / "LaunchAgents" / "com.singularity.nightly-index.plist"
    data = plistlib.loads(plist_path.read_bytes())
    assert data["ProgramArguments"] == ["/usr/bin/py", *ARGS]   # exact, unsplit
