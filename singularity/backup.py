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


def create(cfg: Config, now: str | None = None) -> dict[str, Any]:
    ts = now or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = _backups_dir(cfg) / ts
    dest.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if cfg.config_path.exists():
        shutil.copy2(cfg.config_path, dest / "config.json")
        files["config.json"] = _sha256(dest / "config.json")
    manifest = {
        "id": ts,
        "created_at": ts,
        "machine_id": machine_id(),
        "schema_version": SCHEMA_VERSION,
        "index_generation": _current_generation(cfg),
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

    gen = manifest.get("index_generation")
    index_rolled = False
    if gen and (cfg.memory_index_dir / gen).is_dir():
        cur = cfg.memory_index_dir / "CURRENT"
        tmp = cur.with_name("CURRENT.restore.tmp")
        tmp.write_text(gen, encoding="utf-8")
        tmp.replace(cur)
        index_rolled = True

    return {"ok": True, "restored": restored, "index_rolled_back": index_rolled,
            "id": backup_id}


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
