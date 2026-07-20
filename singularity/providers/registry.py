"""Provider registry: load + validate registry.json (CONTRACT.md §4, §7)."""
from __future__ import annotations

import json
import re
from pathlib import Path

REGISTRY_PATH = Path(__file__).with_name("registry.json")
_NAME_RE = re.compile(r"^[a-z0-9-]+$")

VALID_CLASSES = {"meta-registry", "motion-primitives", "creative-components",
                 "canonical-3d", "timeline-scroll", "inspiration-only",
                 "local-golden"}
VALID_METHODS = {"api", "cli", "public-html"}


class RegistryError(Exception):
    pass


def load_registry(path: Path | None = None) -> dict:
    reg_path = path or REGISTRY_PATH
    try:
        reg = json.loads(reg_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"registry unreadable: {reg_path}: {exc}") from exc
    if reg.get("schema_version") != 1:
        raise RegistryError(
            f"registry schema_version {reg.get('schema_version')!r} != 1")
    if "budgets" not in reg or "providers" not in reg:
        raise RegistryError("registry must contain 'budgets' and 'providers'")
    seen: set[str] = set()
    for prov in reg["providers"]:
        name = prov.get("name") or "<unnamed>"
        if not _NAME_RE.match(name):
            raise RegistryError(f"provider name {name!r} must match [a-z0-9-]+")
        if name in seen:
            raise RegistryError(f"duplicate provider {name}")
        seen.add(name)
        if prov.get("class") not in VALID_CLASSES:
            raise RegistryError(f"provider {name}: bad class {prov.get('class')!r}")
        if prov.get("method") not in VALID_METHODS:
            raise RegistryError(f"provider {name}: bad method {prov.get('method')!r}")
        if not prov.get("adapter"):
            raise RegistryError(f"provider {name}: adapter is required")
        if prov.get("enabled") is False and not prov.get("disabled_reason"):
            raise RegistryError(f"provider {name}: enabled=false requires disabled_reason")
    return reg
