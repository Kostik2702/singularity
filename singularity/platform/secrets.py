"""Secret storage: keyring (DPAPI / Keychain / SecretService) with 0600-file fallback.

Secrets never go to argv, vault or logs (Genevra secret hygiene).
"""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Optional

SERVICE = "singularity"
_FALLBACK = Path.home() / ".singularity-secrets.json"


def _keyring():
    try:
        import keyring
        from keyring.errors import NoKeyringError  # noqa: F401

        keyring.get_keyring()
        return keyring
    except Exception:
        return None


def set_secret(name: str, value: str) -> str:
    kr = _keyring()
    if kr is not None:
        kr.set_password(SERVICE, name, value)
        return "keyring"
    data = {}
    if _FALLBACK.exists():
        data = json.loads(_FALLBACK.read_text(encoding="utf-8"))
    data[name] = value
    _FALLBACK.write_text(json.dumps(data), encoding="utf-8")
    try:
        os.chmod(_FALLBACK, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return "file-fallback"


def get_secret(name: str) -> Optional[str]:
    kr = _keyring()
    if kr is not None:
        val = kr.get_password(SERVICE, name)
        if val is not None:
            return val
    if _FALLBACK.exists():
        try:
            return json.loads(_FALLBACK.read_text(encoding="utf-8")).get(name)
        except json.JSONDecodeError:
            return None
    return None


def backend() -> str:
    return "keyring" if _keyring() is not None else "file-fallback"
