"""Source registry: load + validate sources.json (CONTRACT.md §7).
A new source is a config row, not code; adapters are looked up by name."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ._contract import ContractError

_NAME_RE = re.compile(r"^[a-z0-9-]+$")

REGISTRY_PATH = Path(__file__).with_name("sources.json")

VALID_POLICIES = {"api-free-license", "api-hotlink", "links-only", "per-block"}
VALID_METHODS = {"api", "public-html"}


def load_registry(path: Path | None = None) -> dict:
    reg_path = path or REGISTRY_PATH
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractError(f"registry unreadable: {reg_path}: {exc}") from exc
    if reg.get("schema_version") != 1:
        raise ContractError(
            f"registry schema_version {reg.get('schema_version')!r} != 1 — "
            f"refusing to guess")
    if "budgets" not in reg or "sources" not in reg:
        raise ContractError("registry must contain 'budgets' and 'sources'")
    for src in reg["sources"]:
        name = src.get("name") or "<unnamed>"
        if not _NAME_RE.match(name):
            # the name becomes a thumbs/ filename component — a charset guard
            # here kills the path-traversal class outright (review F11)
            raise ContractError(f"source name {name!r} must match [a-z0-9-]+")
        if src.get("enabled", True) and not src.get("probe_url"):
            raise ContractError(f"source {name}: enabled source requires probe_url")
        if src.get("method") not in VALID_METHODS:
            raise ContractError(f"source {name}: bad method {src.get('method')!r}")
        if src.get("license_policy") not in VALID_POLICIES:
            raise ContractError(
                f"source {name}: bad license_policy {src.get('license_policy')!r}")
        if src.get("enabled") is False and not src.get("disabled_reason"):
            raise ContractError(
                f"source {name}: enabled=false requires disabled_reason (§7)")
    return reg


def source_key(src: dict) -> str | None:
    """API key from key_env, or None. Absent key => typed skipped:no-key."""
    env = src.get("key_env")
    if not env:
        return None
    return os.environ.get(env) or None


def auth_headers(src: dict) -> dict:
    key = source_key(src)
    spec = src.get("auth_header")
    if not key or not spec:
        return {}
    name, _, template = spec.partition(":")
    return {name.strip(): template.strip().format(key=key)}
