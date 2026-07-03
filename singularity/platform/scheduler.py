"""OS scheduler abstraction: register/verify/unregister periodic jobs.

Windows: Task Scheduler (schtasks), StartWhenAvailable via XML re-register.
macOS:   launchd LaunchAgent (ported plist logic from Genevra
         search-daemon-ctl.sh — cron on macOS is a TCC/PATH trap).
linux:   systemd user timer with Persistent=true; cron fallback.

All jobs run the current venv python with an absolute path (Genevra lesson:
schedulers run with a minimal PATH).
"""
from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import current_os
from .proc import python_executable


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
    return subprocess.run(cmd, capture_output=True, text=True)


# --------------------------------------------------------------------------
# Windows — Task Scheduler
# --------------------------------------------------------------------------

def _win_register(job: Job) -> str:
    exe = python_executable(windowed=True)
    action = f'"{exe}" ' + " ".join(job.args)
    if job.daily_at:
        cmd = ["schtasks", "/Create", "/F", "/TN", job.task_id,
               "/TR", action, "/SC", "DAILY", "/ST", job.daily_at]
    elif job.at_login:
        cmd = ["schtasks", "/Create", "/F", "/TN", job.task_id,
               "/TR", action, "/SC", "ONLOGON"]
    else:
        raise ValueError("job needs daily_at or at_login")
    res = _run(cmd)
    if res.returncode != 0:
        return f"FAIL: {res.stderr.strip() or res.stdout.strip()}"
    # StartWhenAvailable (coalescing) is not exposed by schtasks flags; set via PowerShell.
    ps = (f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable; "
          f"Set-ScheduledTask -TaskName '{job.task_id}' -Settings $s")
    res2 = _run(["powershell", "-NoProfile", "-Command", ps])
    if res2.returncode != 0:
        return "WARN: registered, but StartWhenAvailable not set"
    return "OK"


def _win_verify(job: Job) -> dict:
    res = _run(["schtasks", "/Query", "/TN", job.task_id])
    if res.returncode != 0:
        return {"registered": False}
    ps = (f"(Get-ScheduledTask -TaskName '{job.task_id}').Settings.StartWhenAvailable")
    res2 = _run(["powershell", "-NoProfile", "-Command", ps])
    coalescing = "true" in res2.stdout.strip().lower()
    return {"registered": True, "coalescing": coalescing}


def _win_unregister(job: Job) -> None:
    _run(["schtasks", "/Delete", "/F", "/TN", job.task_id])


# --------------------------------------------------------------------------
# macOS — launchd LaunchAgent  (port of Genevra setup-schedule)
# --------------------------------------------------------------------------

def _launch_agent_path(job: Job) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"com.singularity.{job.name}.plist"


def _mac_register(job: Job) -> str:
    plist: dict = {
        "Label": f"com.singularity.{job.name}",
        "ProgramArguments": [python_executable()] + job.args,
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
            "[Unit]\nDescription=Singularity %s\n\n[Service]\nType=oneshot\nExecStart=%s %s\n"
            % (job.name, exe, " ".join(job.args)),
            encoding="utf-8",
        )
        if job.daily_at:
            (d / f"{unit}.timer").write_text(
                "[Unit]\nDescription=Singularity %s timer\n\n[Timer]\nOnCalendar=*-*-* %s:00\n"
                "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
                % (job.name, job.daily_at),
                encoding="utf-8",
            )
            _run(["systemctl", "--user", "daemon-reload"])
            res = _run(["systemctl", "--user", "enable", "--now", f"{unit}.timer"])
            return "OK" if res.returncode == 0 else f"FAIL: {res.stderr.strip()}"
        if job.at_login:
            (d / f"{unit}.service").write_text(
                "[Unit]\nDescription=Singularity %s\n\n[Service]\nExecStart=%s %s\nRestart=on-failure\n\n"
                "[Install]\nWantedBy=default.target\n" % (job.name, exe, " ".join(job.args)),
                encoding="utf-8",
            )
            _run(["systemctl", "--user", "daemon-reload"])
            res = _run(["systemctl", "--user", "enable", "--now", f"{unit}.service"])
            return "OK" if res.returncode == 0 else f"FAIL: {res.stderr.strip()}"
    # cron fallback (idempotent append, ported from Genevra)
    if job.daily_at:
        hh, mm = job.daily_at.split(":")
        line = f"{int(mm)} {int(hh)} * * * {exe} {' '.join(job.args)}  # {job.task_id}"
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
        new = "\n".join(l for l in current.splitlines() if job.task_id not in l) + "\n"
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
