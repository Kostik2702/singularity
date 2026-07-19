"""Normalized candidate contract (CONTRACT.md §1-§3): construction applies
the import gate; validation is the boundary against adapter bugs."""
from __future__ import annotations

from datetime import datetime, timezone

KINDS = {"component", "section", "motion-recipe", "shader", "scene",
         "template", "video-composition", "example"}
FRAMEWORKS = {"static", "react", "next", "three", "r3f", "remotion", "unknown"}
INSTALL_METHODS = {"source", "shadcn", "cli", "npm", "reference-only"}
PRICE_TIERS = {"free", "entitled", "paid"}
CAPABILITIES = {"hero", "scroll-linked", "shader", "camera-path", "particles",
                "text", "transition", "interaction", "media", "background",
                "cursor", "3d", "fallback"}

_REQUIRED_STR = ("candidate_id", "provider", "kind", "name", "source_url")


class CandidateError(Exception):
    """Adapter produced a record outside the contract — adapter bug."""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def import_gate(record: dict) -> tuple[bool, str | None]:
    """§2 hard rules — computed, never adapter-overridable."""
    license_str = (record.get("license") or "").strip().lower()
    if not license_str or license_str == "unknown":
        return False, "license unknown — reference-only until checked"
    if record.get("price_tier") == "paid":
        return False, "paid: report-only until owner gate"
    if record.get("install_method") == "reference-only":
        return False, "reference-only source"
    if "remote-code" in (record.get("risks") or []):
        return False, "requires executing unverified remote code"
    return True, None


def make_candidate(provider: str, native_id: str, *, kind: str, name: str,
                   source_url: str, description: str = "",
                   preview_url: str | None = None,
                   source_revision: str | None = None,
                   license_str: str = "unknown",
                   usage_rights: str = "unknown",
                   price_tier: str = "free",
                   framework: str = "unknown",
                   install_method: str = "reference-only",
                   install_command: str | None = None,
                   dependencies: list[str] | None = None,
                   capabilities: list[str] | None = None,
                   risks: list[str] | None = None,
                   evidence: list[str] | None = None) -> dict:
    caps_in = capabilities or []
    caps = [c for c in caps_in if c in CAPABILITIES]
    record = {
        "candidate_id": f"{provider}/{native_id}",
        "provider": provider,
        "kind": kind,
        "name": (name or "").strip()[:200],
        "description": (description or "").strip()[:1000],
        "source_url": source_url,
        "preview_url": preview_url,
        "retrieved_at": now_iso(),
        "source_revision": source_revision,
        "license": license_str or "unknown",
        "usage_rights": usage_rights,
        "price_tier": price_tier,
        "framework": framework,
        "install_method": install_method,
        "install_command": install_command,
        "dependencies": sorted(dependencies or []),
        "capabilities": sorted(caps),
        "risks": sorted(risks or []),
        "health": "live",
        "stale": False,
        "evidence": list(evidence or []),  # copy: no caller-list aliasing (review G)
    }
    if len(caps) != len(caps_in):
        dropped = sorted(set(caps_in) - set(caps))
        record["evidence"].append(f"dropped unknown capabilities: {dropped}")
    record["importable"], record["import_block_reason"] = import_gate(record)
    validate(record)
    return record


def validate(record: dict) -> None:
    for key in _REQUIRED_STR:
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise CandidateError(f"candidate missing/empty {key!r}: {record.get('candidate_id')}")
    if record["kind"] not in KINDS:
        raise CandidateError(f"bad kind {record['kind']!r} ({record['candidate_id']})")
    if record["framework"] not in FRAMEWORKS:
        raise CandidateError(f"bad framework {record['framework']!r} ({record['candidate_id']})")
    if record["install_method"] not in INSTALL_METHODS:
        raise CandidateError(f"bad install_method {record['install_method']!r} ({record['candidate_id']})")
    if record["price_tier"] not in PRICE_TIERS:
        raise CandidateError(f"bad price_tier {record['price_tier']!r} ({record['candidate_id']})")
    if not record["source_url"].lower().startswith(("http://", "https://")):
        raise CandidateError(f"non-http source_url ({record['candidate_id']})")
    unknown_caps = set(record["capabilities"]) - CAPABILITIES
    if unknown_caps:
        raise CandidateError(f"unknown capabilities {sorted(unknown_caps)} ({record['candidate_id']})")
    if record["importable"]:
        ok, reason = import_gate(record)
        if not ok:
            raise CandidateError(
                f"import gate violation: importable=true but {reason} "
                f"({record['candidate_id']})")
