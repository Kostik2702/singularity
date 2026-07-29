"""mb backup — create/verify/list/restore of critical config + index pointer
(AUD-20).

A backup is a small, checksummed snapshot under .mb/backups/<id>/: the config
file plus a manifest recording the current index generation (index rollback
reuses the AUD-04 generations rather than copying gigabytes of embeddings).
Restore is staged and validated; the last surviving backup is never pruned.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .config import Config, SCHEMA_VERSION, machine_id

RETENTION = 10
MANIFEST = "manifest.json"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _backups_dir(cfg: Config) -> Path:
    return cfg.mb_dir / "backups"


def _current_generation(cfg: Config) -> str | None:
    cur = cfg.memory_index_dir / "CURRENT"
    return cur.read_text(encoding="utf-8").strip() if cur.exists() else None


def _raw_source_files(cfg: Config) -> list[Path]:
    root = cfg.sources_dir
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("*") if p.is_file())


def _sources_bytes(cfg: Config) -> int:
    total = 0
    for path in _raw_source_files(cfg):
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def create(cfg: Config, now: str | None = None,
           with_sources: bool | None = None) -> dict[str, Any]:
    """Snapshot config + the ingest layer.

    WIKI-LOOP D1 makes `.mb/sources/` the ONLY non-reconstructible data in the
    vault: every other thing under `.mb/` can be rebuilt from the notes, raw
    sources cannot. Before W10 this function copied `config.json` and nothing
    else, so the one irreplaceable directory was the one directory not backed
    up. Now the registry and every raw document travel with checksums.

    `with_sources=None` (default) includes raw while it fits under
    `backup_max_sources_mb`, and otherwise SKIPS it with a warning rather than
    silently writing hundreds of megabytes into `.mb/backups/` on every run —
    pass `with_sources=True` to force it."""
    ts = now or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _backups_dir(cfg) / ts
    dest.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    warnings: list[str] = []
    if cfg.config_path.exists():
        shutil.copy2(cfg.config_path, dest / "config.json")
        files["config.json"] = _sha256(dest / "config.json")

    if cfg.sources_registry_path.exists():
        shutil.copy2(cfg.sources_registry_path, dest / "sources.json")
        files["sources.json"] = _sha256(dest / "sources.json")

    raw_files = _raw_source_files(cfg)
    raw_bytes = _sources_bytes(cfg)
    limit_mb = float(cfg.get("backup_max_sources_mb"))
    include_raw = with_sources if with_sources is not None else \
        (raw_bytes <= limit_mb * 1e6)
    if raw_files and not include_raw:
        warnings.append(
            f"raw sources NOT backed up: {raw_bytes / 1e6:.1f} MB exceeds "
            f"backup_max_sources_mb={limit_mb:.0f}. Re-run with --with-sources, "
            f"or evacuate them once with `mb ingest export <dir>`.")
    if include_raw:
        for path in raw_files:
            rel = path.relative_to(cfg.sources_dir).as_posix()
            out = dest / "sources" / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, out)
            files[f"sources/{rel}"] = _sha256(out)

    manifest = {
        "id": ts,
        "created_at": ts,
        "machine_id": machine_id(),
        "schema_version": SCHEMA_VERSION,
        "index_generation": _current_generation(cfg),
        "sources_included": bool(include_raw and raw_files),
        "sources_count": len(raw_files) if include_raw else 0,
        "sources_bytes": raw_bytes if include_raw else 0,
        "warnings": warnings,
        "files": files,
    }
    (dest / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
    _prune(cfg)
    return manifest


def list_backups(cfg: Config) -> list[dict[str, Any]]:
    root = _backups_dir(cfg)
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        mf = d / MANIFEST
        if mf.is_file():
            try:
                out.append(json.loads(mf.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                pass
    return out


def verify(cfg: Config, backup_id: str) -> dict[str, Any]:
    d = _backups_dir(cfg) / backup_id
    mf = d / MANIFEST
    if not mf.is_file():
        return {"ok": False, "error": "backup not found"}
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    bad = []
    for name, sha in manifest.get("files", {}).items():
        f = d / name
        if not f.is_file() or _sha256(f) != sha:
            bad.append(name)
    return {"ok": not bad, "corrupt": bad, "id": backup_id}


def restore(cfg: Config, backup_id: str) -> dict[str, Any]:
    """Validate, snapshot the current config as a safety backup, then restore the
    backup's config and (if it still exists) roll the index CURRENT pointer back
    to the recorded generation."""
    v = verify(cfg, backup_id)
    if not v["ok"]:
        return {"ok": False, "error": f"verify failed: {v.get('error') or v.get('corrupt')}"}
    d = _backups_dir(cfg) / backup_id
    manifest = json.loads((d / MANIFEST).read_text(encoding="utf-8"))

    restored = []
    src_cfg = d / "config.json"
    if src_cfg.is_file():
        create(cfg, now="pre-restore-" + backup_id)     # safety snapshot first
        tmp = cfg.config_path.with_name(cfg.config_path.name + ".restore.tmp")
        shutil.copy2(src_cfg, tmp)
        tmp.replace(cfg.config_path)
        restored.append("config.json")

    # --- ingest layer -----------------------------------------------------
    # Additive ONLY. Raw sources are immutable evidence (D6) and the one thing
    # here with no second copy, so a restore may fill gaps but must never
    # overwrite or delete what is already on disk.
    raw_restored, raw_skipped = 0, 0
    for path in sorted((d / "sources").rglob("*")) if (d / "sources").is_dir() else []:
        if not path.is_file():
            continue
        rel = path.relative_to(d / "sources").as_posix()
        target = cfg.sources_dir / rel
        if target.exists():
            raw_skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        raw_restored += 1
    if raw_restored:
        restored.append(f"sources/ (+{raw_restored})")

    registry_added = 0
    src_registry = d / "sources.json"
    if src_registry.is_file():
        from .config import atomic_write_json
        try:
            backed_up = json.loads(src_registry.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            backed_up = {}
        entries = backed_up.get("sources") if isinstance(backed_up, dict) else None
        if isinstance(entries, dict):
            from . import sources as sources_mod
            current = sources_mod.read_registry(cfg)
            for sid, record in entries.items():
                if sid not in current["sources"]:
                    current["sources"][sid] = record
                    registry_added += 1
            if registry_added or not cfg.sources_registry_path.exists():
                atomic_write_json(cfg.sources_registry_path, current)
                restored.append(f"sources.json (+{registry_added})")

    gen = manifest.get("index_generation")
    index_rolled = False
    if gen and (cfg.memory_index_dir / gen).is_dir():
        cur = cfg.memory_index_dir / "CURRENT"
        tmp = cur.with_name("CURRENT.restore.tmp")
        tmp.write_text(gen, encoding="utf-8")
        tmp.replace(cur)
        index_rolled = True

    return {"ok": True, "restored": restored, "index_rolled_back": index_rolled,
            "sources_restored": raw_restored, "sources_kept": raw_skipped,
            "registry_entries_added": registry_added, "id": backup_id}


def _prune(cfg: Config, keep: int = RETENTION) -> None:
    """Keep the `keep` most recent backups; never delete the last surviving one."""
    root = _backups_dir(cfg)
    dirs = sorted((d for d in root.iterdir() if (d / MANIFEST).is_file()),
                  key=lambda d: d.name)
    excess = len(dirs) - keep
    for d in dirs[:max(excess, 0)]:
        if len(dirs) <= 1:
            break
        shutil.rmtree(d, ignore_errors=True)
