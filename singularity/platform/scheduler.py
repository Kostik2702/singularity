"""OS scheduler abstraction: register/verify/unregister periodic jobs.

Windows: Task Scheduler (schtasks), StartWhenAvailable via XML re-register.
macOS:   launchd LaunchAgent (cron on macOS is a TCC/PATH trap).
linux:   systemd user timer with Persistent=true; cron fallback.

All jobs run the current venv python with an absolute path (a lesson paid for
once: schedulers run with a minimal PATH) and pin a working directory (issue #16:
`-m singularity` puts the CWD on sys.path, and a systemd user unit's default
CWD is $HOME — which the README tells people to clone into as ~/singularity,
so every scheduled run would die on a namespace-shadowed import).
"""
from __future__ import annotations

import os
import plistlib
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape, unescape as _xml_unescape

from . import current_os
from .proc import module_search_root, python_executable


@dataclass
class Job:
    name: str                 # e.g. "nightly-index"
    args: list[str]           # e.g. ["-m", "singularity", "index", "rebuild"]
    daily_at: str | None = None   # "HH:MM" -> daily trigger
    at_login: bool = False

    @property
    def task_id(self) -> str:
        return f"Singularity-{self.name}"


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    # errors="replace": schtasks/launchctl emit bytes in the console/OEM codepage
    # (e.g. cp866/cp1251 localized text) that the locale text decoder chokes on —
    # a raw decode crashes the subprocess reader thread. Never let scheduler I/O
    # raise on undecodable status output.
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace")


# --------------------------------------------------------------------------
# argv serialization — one correct quoting per backend (AUD-01)
#
# The bug: `" ".join(args)` split any argv element containing a space (a vault
# path like `E:\obsidian\memory brain`) into two tokens, so the scheduled job
# received a broken --vault and failed with LastTaskResult=1. Each OS parses
# the stored action differently, so each needs its own quoter.
# --------------------------------------------------------------------------

def _win_arguments(args: list[str]) -> str:
    """argv -> a single Windows Arguments string that CommandLineToArgvW (used
    by the launched program) splits back into the exact same argv."""
    return subprocess.list2cmdline(list(args))


def _win_task_xml(exe: str, args: list[str], *,
                  daily_at: str | None = None, at_login: bool = False) -> str:
    """Task Scheduler definition XML. Command and Arguments are separate fields
    (no schtasks /TR quote-mangling), StartWhenAvailable is set inline (no extra
    PowerShell round-trip)."""
    if daily_at:
        hh, mm = daily_at.split(":")
        trigger = (
            "    <CalendarTrigger>\n"
            f"      <StartBoundary>2020-01-01T{int(hh):02d}:{int(mm):02d}:00</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>\n"
            "    </CalendarTrigger>\n"
        )
    elif at_login:
        trigger = "    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>\n"
    else:
        raise ValueError("job needs daily_at or at_login")
    arguments = _win_arguments(args)
    workdir = module_search_root()
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" '
        'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <Triggers>\n" + trigger + "  </Triggers>\n"
        "  <Settings>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        "  <Actions>\n"
        "    <Exec>\n"
        f"      <Command>{_xml_escape(exe)}</Command>\n"
        f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{_xml_escape(workdir)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _systemd_quote(arg: str) -> str:
    """Quote one arg for a systemd ExecStart= line: double-quote, escape
    backslash and quote, double literal percent (systemd specifier char)."""
    s = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{s}"'


def _systemd_execstart(exe: str, args: list[str]) -> str:
    return " ".join(_systemd_quote(a) for a in [exe, *args])


def _cron_command(exe: str, args: list[str]) -> str:
    """Quote for a crontab line (executed via /bin/sh).

    cron runs the job from $HOME, which is where the README tells people to
    clone (`~/singularity`) — so the `cd` is not cosmetic, it is what keeps
    `-m singularity` from importing the clone directory as a namespace
    package (issue #16)."""
    cmd = " ".join(shlex.quote(a) for a in [exe, *args])
    return f"cd {shlex.quote(module_search_root())} && {cmd}"


def expected_arguments(job: Job) -> str | None:
    """The Arguments string verify() should read back for this job, so a caller
    (mb doctor, AUD-02) can detect a stale/broken stored action. Windows only —
    schtasks stores Arguments as a separate field; None on other OSes where
    verify() does not expose a comparable read-back."""
    if current_os() == "windows":
        return _win_arguments(job.args)
    return None


# --------------------------------------------------------------------------
# Windows — Task Scheduler
# --------------------------------------------------------------------------

def _win_register(job: Job) -> str:
    # Register from an XML definition (schtasks /Create /XML). This keeps
    # Command/Arguments as separate, un-mangled fields — /TR string quoting
    # cannot survive an argument that contains a space — and sets
    # StartWhenAvailable in the same step. Overwrites any prior (possibly
    # broken) registration idempotently via /F.
    exe = python_executable(windowed=True)
    xml = _win_task_xml(exe, job.args, daily_at=job.daily_at, at_login=job.at_login)
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="mb-task-")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:   # schtasks wants UTF-16
            f.write(xml)
        res = _run(["schtasks", "/Create", "/F", "/TN", job.task_id, "/XML", path])
    finally:
        os.unlink(path)
    if res.returncode != 0:
        return f"FAIL: {res.stderr.strip() or res.stdout.strip()}"
    return "OK"


def _xml_first(text: str, tag: str) -> str | None:
    # Task Scheduler may emit quotes either literally or as &quot; — unescape both.
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return _xml_unescape(m.group(1), {"&quot;": '"', "&apos;": "'"}) if m else None


def _win_verify(job: Job) -> dict:
    # Read the task back as XML and report the ACTUAL stored command/arguments
    # so callers (mb doctor, AUD-02) can compare against what was intended.
    res = _run(["schtasks", "/Query", "/TN", job.task_id, "/XML"])
    if res.returncode != 0:
        return {"registered": False}
    xml = res.stdout
    compact = re.sub(r"\s+", "", xml)
    return {
        "registered": True,
        "coalescing": "<StartWhenAvailable>true</StartWhenAvailable>" in compact,
        "command": _xml_first(xml, "Command"),
        "arguments": _xml_first(xml, "Arguments"),
    }


def _win_unregister(job: Job) -> None:
    _run(["schtasks", "/Delete", "/F", "/TN", job.task_id])


# --------------------------------------------------------------------------
# macOS — launchd LaunchAgent
# --------------------------------------------------------------------------

def _launch_agent_path(job: Job) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.singularity.{job.name}.plist"


def _mac_register(job: Job) -> str:
    plist: dict = {
        "Label": f"com.singularity.{job.name}",
        "ProgramArguments": [python_executable()] + job.args,
        "WorkingDirectory": module_search_root(),
        "StandardOutPath": str(Path.home() / f".singularity-{job.name}.log"),
        "StandardErrorPath": str(Path.home() / f".singularity-{job.name}.log"),
    }
    if job.daily_at:
        hh, mm = job.daily_at.split(":")
        plist["StartCalendarInterval"] = {"Hour": int(hh), "Minute": int(mm)}
    if job.at_login:
        plist["RunAtLoad"] = True
    path = _launch_agent_path(job)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    uid = _run(["id", "-u"]).stdout.strip()
    _run(["launchctl", "bootout", f"gui/{uid}", str(path)])   # idempotent re-register
    res = _run(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
    return "OK" if res.returncode == 0 else f"FAIL: {res.stderr.strip()}"


def _mac_verify(job: Job) -> dict:
    uid = _run(["id", "-u"]).stdout.strip()
    res = _run(["launchctl", "print", f"gui/{uid}/com.singularity.{job.name}"])
    # launchd coalesces missed StartCalendarInterval runs natively
    return {"registered": res.returncode == 0, "coalescing": res.returncode == 0}


def _mac_unregister(job: Job) -> None:
    uid = _run(["id", "-u"]).stdout.strip()
    path = _launch_agent_path(job)
    _run(["launchctl", "bootout", f"gui/{uid}", str(path)])
    path.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Linux — systemd user timer (Persistent=true); cron fallback
# --------------------------------------------------------------------------

def _systemd_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _linux_has_systemd() -> bool:
    return _run(["systemctl", "--user", "--version"]).returncode == 0


def _linux_register(job: Job) -> str:
    exe = python_executable()
    if _linux_has_systemd():
        unit = f"singularity-{job.name}"
        d = _systemd_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{unit}.service").write_text(
            f"[Unit]\nDescription=Singularity {job.name}\n\n[Service]\nType=oneshot\n"
            f"WorkingDirectory={_systemd_quote(module_search_root())}\nExecStart={_systemd_execstart(exe, job.args)}\n",
            encoding="utf-8",
        )
        if job.daily_at:
            (d / f"{unit}.timer").write_text(
                f"[Unit]\nDescription=Singularity {job.name} timer\n\n[Timer]\nOnCalendar=*-*-* {job.daily_at}:00\n"
                "Persistent=true\n\n[Install]\nWantedBy=timers.target\n",
                encoding="utf-8",
            )
            _run(["systemctl", "--user", "daemon-reload"])
            res = _run(["systemctl", "--user", "enable", "--now", f"{unit}.timer"])
            return "OK" if res.returncode == 0 else f"FAIL: {res.stderr.strip()}"
        if job.at_login:
            (d / f"{unit}.service").write_text(
                f"[Unit]\nDescription=Singularity {job.name}\n\n[Service]\n"
                f"WorkingDirectory={_systemd_quote(module_search_root())}\nExecStart={_systemd_execstart(exe, job.args)}\nRestart=on-failure\n\n"
                "[Install]\nWantedBy=default.target\n",
                encoding="utf-8",
            )
            _run(["systemctl", "--user", "daemon-reload"])
            res = _run(["systemctl", "--user", "enable", "--now", f"{unit}.service"])
            return "OK" if res.returncode == 0 else f"FAIL: {res.stderr.strip()}"
    # cron fallback (idempotent append)
    if job.daily_at:
        hh, mm = job.daily_at.split(":")
        line = f"{int(mm)} {int(hh)} * * * {_cron_command(exe, job.args)}  # {job.task_id}"
        current = _run(["crontab", "-l"]).stdout
        if job.task_id not in current:
            new = current.rstrip() + "\n" + line + "\n"
            subprocess.run(["crontab", "-"], input=new, text=True)
        return "OK (cron fallback — no Persistent catch-up)"
    return "FAIL: no systemd; at_login job unsupported via cron"


def _linux_verify(job: Job) -> dict:
    if _linux_has_systemd():
        unit = f"singularity-{job.name}"
        res = _run(["systemctl", "--user", "is-enabled",
                    f"{unit}.timer" if job.daily_at else f"{unit}.service"])
        ok = res.returncode == 0
        persistent = False
        tpath = _systemd_dir() / f"{unit}.timer"
        if tpath.exists():
            persistent = "Persistent=true" in tpath.read_text(encoding="utf-8")
        return {"registered": ok, "coalescing": persistent}
    current = _run(["crontab", "-l"]).stdout
    return {"registered": job.task_id in current, "coalescing": False}


def _linux_unregister(job: Job) -> None:
    if _linux_has_systemd():
        unit = f"singularity-{job.name}"
        _run(["systemctl", "--user", "disable", "--now", f"{unit}.timer"])
        _run(["systemctl", "--user", "disable", "--now", f"{unit}.service"])
        for suffix in (".timer", ".service"):
            (_systemd_dir() / f"{unit}{suffix}").unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    current = _run(["crontab", "-l"]).stdout
    if job.task_id in current:
        new = "\n".join(ln for ln in current.splitlines() if job.task_id not in ln) + "\n"
        subprocess.run(["crontab", "-"], input=new, text=True)


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

_IMPL = {
    "windows": (_win_register, _win_verify, _win_unregister),
    "macos": (_mac_register, _mac_verify, _mac_unregister),
    "linux": (_linux_register, _linux_verify, _linux_unregister),
}


def register(job: Job) -> str:
    return _IMPL[current_os()][0](job)


def verify(job: Job) -> dict:
    return _IMPL[current_os()][1](job)


def unregister(job: Job) -> None:
    _IMPL[current_os()][2](job)
