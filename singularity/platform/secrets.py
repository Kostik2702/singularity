"""Secret storage: keyring (DPAPI / Keychain / SecretService) with an opt-in,
atomic 0600-file fallback.

Secrets never go to argv, vault or logs (secret hygiene). The plaintext
file fallback is refused unless the caller explicitly opts in — an unavailable
keyring must not silently downgrade to storing secrets in clear text.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import shutil
import stat
import time
from pathlib import Path

from .fslock import make_lock_dir

SERVICE = "singularity"
_FALLBACK = Path.home() / ".singularity-secrets.json"
_HEALTH_PROBE = "__mb_healthprobe__"
_LOCK_STALE_SECONDS = 60


class SecretsError(RuntimeError):
    """No secure backend available and insecure fallback was not permitted."""


def _keyring():
    """Return a keyring module only if it is actually usable — a real read probe
    catches a `fail`/misconfigured backend that survives get_keyring()."""
    try:
        import keyring

        keyring.get_keyring()
        keyring.get_password(SERVICE, _HEALTH_PROBE)   # raises on a dead backend
        return keyring
    except Exception:
        return None


# --- fallback file (opt-in, atomic, 0600) -----------------------------------

def _lock_path() -> Path:
    return _FALLBACK.with_name(_FALLBACK.name + ".lock")


def _names_path() -> Path:
    # names-only registry (no values) so list/delete work across BOTH backends —
    # the OS keyring cannot enumerate its own entries portably.
    return _FALLBACK.with_name(_FALLBACK.name + ".names")


def _read_names() -> set[str]:
    p = _names_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (json.JSONDecodeError, OSError):
        return set()


def _write_names(names: set[str]) -> None:
    p = _names_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(sorted(names)), encoding="utf-8")
    os.replace(tmp, p)


@contextlib.contextmanager
def _file_lock():
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        if not make_lock_dir(lock):
            if time.time() - lock.stat().st_mtime > _LOCK_STALE_SECONDS:
                pass                       # stale -> take over
            else:
                raise SecretsError(
                    "secrets file is locked by another process")
        yield
    finally:
        shutil.rmtree(lock, ignore_errors=True)


def _read_fallback() -> dict:
    if not _FALLBACK.exists():
        return {}
    try:
        data = json.loads(_FALLBACK.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}                          # corrupt -> treat as empty, never crash


def _backup_corrupt() -> None:
    try:
        json.loads(_FALLBACK.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with contextlib.suppress(OSError):
            shutil.move(str(_FALLBACK),
                        str(_FALLBACK.with_name(f"{_FALLBACK.name}.corrupt-{ts}")))


def _write_fallback_atomic(data: dict) -> None:
    """Write with mode 0600 established BEFORE any content is written, then
    atomically replace — the secret is never briefly world-readable."""
    _FALLBACK.parent.mkdir(parents=True, exist_ok=True)
    tmp = _FALLBACK.with_name(f"{_FALLBACK.name}.{os.getpid()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600)        # perms at creation, not after
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, _FALLBACK)


# --- public API --------------------------------------------------------------

def set_secret(name: str, value: str, *, allow_plaintext: bool = False) -> str:
    """Store a secret. Uses the OS keyring when available; falls back to a
    0600 plaintext file ONLY when allow_plaintext=True, else raises."""
    kr = _keyring()
    if kr is not None:
        kr.set_password(SERVICE, name, value)
        _write_names(_read_names() | {name})
        return "keyring"
    if not allow_plaintext:
        raise SecretsError(
            "no secure secret backend (keyring) available; refusing to store a "
            "secret in plaintext. Pass allow_plaintext=True to override.")
    with _file_lock():
        _backup_corrupt()
        data = _read_fallback()
        data[name] = value
        _write_fallback_atomic(data)
        _write_names(_read_names() | {name})
    return "file-fallback"


def get_secret(name: str) -> str | None:
    kr = _keyring()
    if kr is not None:
        val = kr.get_password(SERVICE, name)
        if val is not None:
            return val
    return _read_fallback().get(name)


def delete_secret(name: str) -> bool:
    """Remove a secret from whichever backend holds it. Returns True if removed."""
    removed = False
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, name)
            removed = True
        except Exception:
            pass
    with _file_lock():
        data = _read_fallback()
        if name in data:
            del data[name]
            _write_fallback_atomic(data)
            removed = True
        names = _read_names()
        if name in names:
            names.discard(name)
            _write_names(names)
            removed = True
    return removed


def list_secrets() -> list[str]:
    """Names only — values are never returned or logged. Backed by a names
    registry so keyring-stored secrets are listed too (keyring cannot enumerate
    itself), unioned with any fallback-file keys."""
    return sorted(_read_names() | set(_read_fallback().keys()))


def backend() -> str:
    return "keyring" if _keyring() is not None else "file-fallback"
