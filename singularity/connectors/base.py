"""Connector contract (AUD-22).

A connector is an external-source plugin with a declared, inspectable contract:
capabilities, a config schema, secret REFERENCES (names resolved via the secrets
backend — never values in argv/log/vault), a health check, an incremental
cursor, and a normalized document shape. Connectors are OFF by default; a real
cloud integration is added only after a local reference connector and these
contract tests pass.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class ConnectorManifest:
    name: str
    capabilities: list[str] = field(default_factory=list)   # e.g. ["fetch", "incremental"]
    config_schema: dict[str, str] = field(default_factory=dict)  # key -> type name
    secret_refs: list[str] = field(default_factory=list)    # secret NAMES, never values
    rate_limit_per_min: Optional[int] = None
    permissions: list[str] = field(default_factory=list)    # e.g. ["read"]


@dataclass
class NormalizedDoc:
    id: str
    title: str
    text: str
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Connector(Protocol):
    def manifest(self) -> ConnectorManifest: ...

    def health(self) -> dict[str, Any]:
        """Cheap liveness/config check: {"ok": bool, "detail": str}."""
        ...

    def fetch(self, cursor: Optional[str] = None
              ) -> tuple[list[NormalizedDoc], Optional[str]]:
        """Return (docs since cursor, next_cursor). Incremental via the opaque
        cursor; a None cursor means a full initial fetch."""
        ...


def validate_manifest(m: ConnectorManifest) -> list[str]:
    """Contract checks every connector's manifest must satisfy."""
    errors = []
    if not m.name or not m.name.strip():
        errors.append("name: must be non-empty")
    if not isinstance(m.capabilities, list):
        errors.append("capabilities: must be a list")
    if not isinstance(m.secret_refs, list) or not all(isinstance(s, str) for s in m.secret_refs):
        errors.append("secret_refs: must be a list of secret NAMES")
    if m.rate_limit_per_min is not None and (
            not isinstance(m.rate_limit_per_min, int) or m.rate_limit_per_min <= 0):
        errors.append("rate_limit_per_min: must be a positive int or None")
    return errors
