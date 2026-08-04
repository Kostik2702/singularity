"""Connector registry (AUD-22): connectors are OFF by default and only
instantiated when explicitly enabled in config `connectors.<name>.enabled`.
"""
from __future__ import annotations

from typing import Any
from collections.abc import Callable

from ..config import Config
from .base import Connector
from .local_files import LocalFilesConnector

# name -> factory(config-section) -> Connector
_FACTORIES: dict[str, Callable[[dict[str, Any]], Connector]] = {
    "local_files": lambda sec: LocalFilesConnector(sec.get("root", ".")),
}


def available() -> list[str]:
    return sorted(_FACTORIES)


def is_enabled(cfg: Config, name: str) -> bool:
    sec = (cfg.get("connectors") or {}).get(name) or {}
    return bool(sec.get("enabled"))


def enabled(cfg: Config) -> list[str]:
    return [n for n in _FACTORIES if is_enabled(cfg, n)]


def load(cfg: Config, name: str) -> Connector:
    """Instantiate an ENABLED connector. Raises if unknown or disabled — a
    connector is never activated implicitly."""
    if name not in _FACTORIES:
        raise KeyError(f"unknown connector: {name}")
    if not is_enabled(cfg, name):
        raise PermissionError(f"connector '{name}' is disabled (off by default)")
    sec = (cfg.get("connectors") or {}).get(name) or {}
    return _FACTORIES[name](sec)
