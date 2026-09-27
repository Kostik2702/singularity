"""`platform.fslock.make_lock_dir`: the engine's directory locks, including the
Windows moment when a lock being released refuses `mkdir` as ACCESS DENIED.

Found by the CI matrix (test_ingest_log, windows-latest): a diagnostic run on
the runners then showed 10-18 PermissionErrors per 3000 registry-lock
acquisitions, every one from the lock's `mkdir`. A desktop Windows 11 never
reproduced it, so these tests simulate the refusal rather than wait for it.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import pytest

from singularity.config import Config
from singularity.knowledge import sources
from singularity.platform import fslock

ENGINE = Path(__file__).resolve().parents[1]


def _refusing(monkeypatch, lock: Path, times: int) -> list[int]:
    """`mkdir` of `lock` refused as delete-pending `times` times, then real."""
    calls = [0]
    real = Path.mkdir

    def mkdir(self, *a, **k):
        if self == lock:
            calls[0] += 1
            if calls[0] <= times:
                raise PermissionError(13, "Access is denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    return calls


def test_a_lock_being_released_on_windows_is_waited_for_not_raised(tmp_path, monkeypatch):
    """Planted: the PermissionError branch removed -- the refusal escapes, red."""
    monkeypatch.setattr(fslock, "current_os", lambda: "windows")
    lock = tmp_path / "x.lock"
    calls = _refusing(monkeypatch, lock, times=3)
    assert fslock.make_lock_dir(lock) is True
    assert calls[0] == 4 and lock.is_dir()


def test_a_held_lock_is_reported_taken(tmp_path):
    lock = tmp_path / "x.lock"
    lock.mkdir()
    assert fslock.make_lock_dir(lock) is False


def test_a_refusal_that_outlasts_delete_pending_is_a_real_one(tmp_path, monkeypatch):
    """A directory we may not create must not read as "someone holds it"
    forever. Planted: the deadline check removed -- the call never returns
    (the watchdog below turns that into red)."""
    monkeypatch.setattr(fslock, "current_os", lambda: "windows")
    monkeypatch.setattr(fslock, "PENDING_SECONDS", 0.05)
    lock = tmp_path / "x.lock"
    _refusing(monkeypatch, lock, times=10**9)
    outcome: list[BaseException | bool] = []
    t = threading.Thread(target=lambda: outcome.append(_call(lock)), daemon=True)
    t.start()
    t.join(10)
    assert outcome and isinstance(outcome[0], PermissionError), outcome or "still retrying after 10 s"


def _call(lock):
    try:
        return fslock.make_lock_dir(lock)
    except BaseException as exc:  # noqa: BLE001
        return exc


def test_off_windows_a_permission_error_is_raised_at_once(tmp_path, monkeypatch):
    """Only Windows has delete-pending; elsewhere EACCES means what it says."""
    monkeypatch.setattr(fslock, "current_os", lambda: "linux")
    lock = tmp_path / "x.lock"
    calls = _refusing(monkeypatch, lock, times=1)
    with pytest.raises(PermissionError):
        fslock.make_lock_dir(lock)
    assert calls[0] == 1


def test_the_ingest_registry_waits_out_a_lock_being_released(tmp_path, monkeypatch):
    """The failure CI found, end to end: the mark is recorded, not raised."""
    monkeypatch.setattr(fslock, "current_os", lambda: "windows")
    vault = tmp_path / "vault"
    (vault / "reference").mkdir(parents=True)
    cfg = Config(vault=vault, data={})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    sid = sources.add(cfg, "# Source\n\n## About\n" + "search index vault note " * 20,
                      origin_ref="D:/docs/a.md")["source_id"]
    _refusing(monkeypatch, cfg.ingest_lock_dir, times=2)
    sources.attach_notes(cfg, sid, ["reference/n.md"], log=True)
    assert sources.get(cfg, sid)["derived_notes"] == ["reference/n.md"]


def test_every_directory_lock_goes_through_make_lock_dir():
    """The class, not the instance: four locks had the same `mkdir`, and the
    CI run caught one of them. A new raw `mkdir(exist_ok=False)` in the engine
    would bring the Windows refusal back. Planted: the ingest registry lock
    back on `lock.mkdir(exist_ok=False)` -- red."""
    raw = re.compile(r"\.mkdir\(\s*exist_ok\s*=\s*False\s*\)")
    offenders = []
    for path in sorted((ENGINE / "singularity").rglob("*.py")):
        if path.name == "fslock.py" and path.parent.name == "platform":
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if raw.search(line.split("#", 1)[0]):
                offenders.append(f"{path.relative_to(ENGINE).as_posix()}:{n}")
    assert offenders == [], f"use platform.fslock.make_lock_dir: {offenders}"
