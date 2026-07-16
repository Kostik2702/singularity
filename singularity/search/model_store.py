"""Explicit lifecycle for the local embedding-model snapshot.

Runtime code never resolves a Hub repository ID.  The installer, ``mb model
install`` and heavy doctor repair are the only callers allowed to download;
they persist the resolved snapshot path/revision in an atomic manifest.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import Config, atomic_write_json

MANIFEST_NAME = "singularity-model.json"
_REQUIRED_FILES = ("modules.json", "config.json")
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin")


class ModelNotInstalledError(RuntimeError):
    """The configured embedding model has no complete local snapshot."""


def manifest_path(cfg: Config) -> Path:
    return cfg.hf_cache / MANIFEST_NAME


def _snapshot_download(**kwargs) -> str:
    """Lazy import: doctor status/runtime checks stay dependency-light."""
    from huggingface_hub import snapshot_download

    return snapshot_download(**kwargs)


def _snapshot_ref(cfg: Config, snapshot: Path) -> str:
    snapshot = snapshot.resolve()
    try:
        return snapshot.relative_to(cfg.hf_cache.resolve()).as_posix()
    except ValueError:  # explicitly configured cache/snapshot on another drive
        return str(snapshot)


def write_manifest(cfg: Config, snapshot: Path, revision: str) -> None:
    atomic_write_json(manifest_path(cfg), {
        "schema": 1,
        "model": cfg.model,
        "snapshot": _snapshot_ref(cfg, snapshot),
        "revision": revision,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _read_manifest(cfg: Config) -> dict[str, Any] | None:
    import json

    try:
        data = json.loads(manifest_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _manifest_snapshot(cfg: Config, data: dict[str, Any]) -> Path | None:
    raw = data.get("snapshot")
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (cfg.hf_cache / path).resolve()


def _missing_snapshot_files(snapshot: Path) -> list[str]:
    missing = [name for name in _REQUIRED_FILES if not (snapshot / name).exists()]
    if not any((snapshot / name).exists() for name in _WEIGHT_FILES):
        missing.append("model weights")
    return missing


def status(cfg: Config) -> dict[str, Any]:
    data = _read_manifest(cfg)
    if data is None:
        return {"installed": False, "model": cfg.model,
                "cache": str(cfg.hf_cache), "reason": "manifest missing"}
    if data.get("model") != cfg.model:
        return {"installed": False, "model": cfg.model,
                "cache": str(cfg.hf_cache), "reason": "manifest model mismatch"}
    snapshot = _manifest_snapshot(cfg, data)
    if snapshot is None or not snapshot.is_dir():
        return {"installed": False, "model": cfg.model,
                "cache": str(cfg.hf_cache), "reason": "snapshot missing"}
    missing = _missing_snapshot_files(snapshot)
    if missing:
        return {"installed": False, "model": cfg.model,
                "cache": str(cfg.hf_cache), "snapshot": str(snapshot),
                "reason": "missing: " + ", ".join(missing)}
    return {"installed": True, "model": cfg.model,
            "cache": str(cfg.hf_cache), "snapshot": str(snapshot),
            "revision": str(data.get("revision") or snapshot.name)}


def require_snapshot(cfg: Config) -> Path:
    state = status(cfg)
    if not state["installed"]:
        raise ModelNotInstalledError(
            f"embedding model {cfg.model!r} is not installed locally at "
            f"{cfg.hf_cache}: {state['reason']}. Run `mb model install` or "
            "`mb doctor --fix --heavy`.")
    return Path(state["snapshot"])


def install(cfg: Config, revision: str | None = None, force: bool = False) -> dict[str, Any]:
    """Adopt a complete local cache or explicitly download one.

    The first probe is strictly local.  Only this explicit install function is
    allowed to retry online, so search/index can never silently touch the Hub.
    """
    current = status(cfg)
    if current["installed"] and not force and revision in (None, current["revision"]):
        return {**current, "source": "manifest", "changed": False}

    cfg.hf_cache.mkdir(parents=True, exist_ok=True)
    kwargs = {"repo_id": cfg.model, "cache_dir": str(cfg.hf_cache),
              "revision": revision}
    try:
        snapshot = Path(_snapshot_download(**kwargs, local_files_only=True))
        source = "local-cache"
    except Exception:  # local probe only; explicit install may now use network
        snapshot = Path(_snapshot_download(**kwargs, local_files_only=False))
        source = "download"

    missing = _missing_snapshot_files(snapshot)
    if missing:
        raise ModelNotInstalledError(
            f"downloaded snapshot is incomplete at {snapshot}: {', '.join(missing)}")
    resolved_revision = snapshot.name
    write_manifest(cfg, snapshot, resolved_revision)
    return {**status(cfg), "source": source, "changed": True}
