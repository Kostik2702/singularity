"""Configuration: vault discovery + .mb/config.json.

Vault resolution order (first hit wins):
  1. explicit --vault CLI argument
  2. env MB_HOME
  3. pointer file ~/.singularity.json  -> {"vault_path": "..."}
  4. built-in dev-machine default — ONLY if that path exists on this machine
     (github#1: on macOS the bare Windows default leaked into the error
     message; with nothing configured resolve_vault now returns None and
     callers show actionable guidance instead)

All runtime state lives inside the vault under `.mb/` so the vault is
self-contained and portable across machines.
"""
from __future__ import annotations

import json
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


def machine_id() -> str:
    """Stable per-machine identifier (hostname-derived) so machine-local runtime
    state does not collide across machines that sync the same vault (AUD-19)."""
    node = platform.node() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", node)[:64] or "unknown"

DEFAULT_VAULT_WINDOWS = r"D:\obsidian\memory brain"
POINTER_FILE = Path.home() / ".singularity.json"

SCHEMA_VERSION = 1

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "min_score": 0.30,
    "daemon_port": 0,          # 0 = OS picks a free port; actual port -> .mb/daemon.port
    "excerpt_chars": 150,
    "batch_size": 64,
    "code_repos_dir": None,     # optional code index root
    "auto_capture": True,
    "brain_max_lines": 40,             # T06: per-project brain note truncation
    "session_context_max_lines": 200,  # T06: total SessionStart injection budget
    "connectors": {},
    "skip_dirs": [".obsidian", ".mb", ".git", ".trash", "templates"],
    "skip_path_parts": ["learnings/archive"],
}

# keys that carry machine-specific absolute paths (validated as strings only —
# their existence is a per-machine concern, not a config-schema error)
_PATH_KEYS = {"code_repos_dir", "hf_cache"}


def validate_config(data: dict[str, Any]) -> dict[str, list[str]]:
    """Validate a root config dict WITHOUT side effects. Returns
    {"errors": [...], "warnings": [...]}: type/range violations are errors,
    unknown keys are warnings (ignored at runtime, never fatal)."""
    errors: list[str] = []

    def _num(key: str, lo: float, hi: float) -> None:
        if key not in data:
            return
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            errors.append(f"{key}: expected a number, got {type(v).__name__}")
        elif not (lo <= v <= hi):
            errors.append(f"{key}: {v!r} out of range [{lo}, {hi}]")

    def _int(key: str, lo: int, hi: int) -> None:
        if key not in data:
            return
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, int):
            errors.append(f"{key}: expected an integer, got {type(v).__name__}")
        elif not (lo <= v <= hi):
            errors.append(f"{key}: {v!r} out of range [{lo}, {hi}]")

    if "model" in data and (not isinstance(data["model"], str) or not data["model"].strip()):
        errors.append("model: must be a non-empty string")
    _num("min_score", 0.0, 1.0)
    _int("daemon_port", 0, 65535)
    _int("excerpt_chars", 1, 1_000_000)
    _int("batch_size", 1, 1_000_000)
    _int("brain_max_lines", 1, 10_000)
    _int("session_context_max_lines", 1, 10_000)
    for key in ("skip_dirs", "skip_path_parts"):
        if key in data and (not isinstance(data[key], list)
                            or not all(isinstance(x, str) for x in data[key])):
            errors.append(f"{key}: must be a list of strings")
    if "auto_capture" in data and not isinstance(data["auto_capture"], bool):
        errors.append("auto_capture: must be a boolean")
    if "connectors" in data and not isinstance(data["connectors"], dict):
        errors.append("connectors: must be an object")
    for key in _PATH_KEYS:
        if key in data and data[key] is not None and not isinstance(data[key], str):
            errors.append(f"{key}: must be a path string or null")

    from .projects import validate_projects
    proj_result = validate_projects(data)
    errors.extend(proj_result["errors"])

    known = set(DEFAULT_CONFIG) | _PATH_KEYS | {"llm", "projects"}
    warnings = [f"{k}: unknown config key (ignored)" for k in data if k not in known]
    warnings.extend(proj_result["warnings"])
    return {"errors": errors, "warnings": warnings}


def migrate_config(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Upgrade an older config in-memory to the current schema. Returns
    (data, changed). Backward-compatible: unknown/extra keys are preserved."""
    changed = False
    data = dict(data)
    if data.get("schema_version") != SCHEMA_VERSION:
        data["schema_version"] = SCHEMA_VERSION
        changed = True
    # future version-keyed migrations go here
    return data, changed


@dataclass
class Config:
    vault: Path
    data: dict[str, Any] = field(default_factory=dict)

    # --- derived paths -------------------------------------------------
    @property
    def mb_dir(self) -> Path:
        return self.vault / ".mb"

    @property
    def config_path(self) -> Path:
        return self.mb_dir / "config.json"

    @property
    def memory_index_dir(self) -> Path:
        return self.mb_dir / "memory_index"

    @property
    def code_index_dir(self) -> Path:
        return self.mb_dir / "code_index"

    @property
    def hf_cache(self) -> Path:
        return Path(self.data.get("hf_cache") or (self.mb_dir / "hf-cache"))

    @property
    def machine_dir(self) -> Path:
        # machine-local runtime state (daemon pid/port/log, heartbeat) — kept
        # OUT of the shared .mb root so two machines syncing this vault don't
        # clobber each other's runtime (AUD-19). Portable config/index stay in .mb.
        return self.mb_dir / "machines" / machine_id()

    @property
    def daemon_port_file(self) -> Path:
        return self.machine_dir / "daemon.port"

    @property
    def daemon_pid_file(self) -> Path:
        return self.machine_dir / "daemon.pid"

    @property
    def daemon_log_file(self) -> Path:
        return self.machine_dir / "daemon.log"

    @property
    def daemon_state_file(self) -> Path:
        # atomic single-file daemon identity: {instance_id, pid, port,
        # create_time, python, started_at, machine_id}
        return self.machine_dir / "daemon.json"

    @property
    def daemon_start_lock(self) -> Path:
        return self.machine_dir / "daemon.start.lock"

    @property
    def watch_heartbeat_file(self) -> Path:
        return self.machine_dir / "watch.heartbeat"

    @property
    def markers_dir(self) -> Path:
        return self.mb_dir / "markers"

    @property
    def lock_dir(self) -> Path:
        # rebuild lock guards the SHARED index -> stays in .mb (not per-machine)
        return self.mb_dir / "rebuild.lock"

    # --- config values --------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        if key in self.data:
            return self.data[key]
        if key in DEFAULT_CONFIG:
            return DEFAULT_CONFIG[key]
        return default

    @property
    def model(self) -> str:
        return self.get("model")

    @property
    def min_score(self) -> float:
        return float(self.get("min_score"))

    def save(self) -> None:
        self.mb_dir.mkdir(parents=True, exist_ok=True)
        merged = {**DEFAULT_CONFIG, **self.data}
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        tmp.replace(self.config_path)

    def apply_env(self) -> None:
        """Point HuggingFace cache at the configured location (keep C: clean)
        and trust the OS certificate store (corporate/AV TLS interception)."""
        os.environ.setdefault("HF_HOME", str(self.hf_cache))
        os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(self.hf_cache))
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass


def resolve_vault(explicit: Optional[str] = None) -> Optional[Path]:
    if explicit:
        return Path(explicit)
    env = os.environ.get("MB_HOME")
    if env:
        return Path(env)
    if POINTER_FILE.exists():
        try:
            data = json.loads(POINTER_FILE.read_text(encoding="utf-8"))
            if data.get("vault_path"):
                return Path(data["vault_path"])
        except (json.JSONDecodeError, OSError):
            pass
    default = Path(DEFAULT_VAULT_WINDOWS)
    if default.exists():
        return default
    return None


def load_config(vault_arg: Optional[str] = None) -> Config:
    vault = resolve_vault(vault_arg)
    if vault is None:
        print("[mb] no vault configured", file=sys.stderr)
        print('[mb] pass --vault "/path/to/your/vault", set MB_HOME, or create '
              '~/.singularity.json with {"vault_path": "/path/to/your/vault"}',
              file=sys.stderr)
        sys.exit(2)
    if not vault.exists():
        print(f"[mb] vault not found: {vault}", file=sys.stderr)
        print("[mb] set MB_HOME, pass --vault, or create ~/.singularity.json", file=sys.stderr)
        sys.exit(2)
    cfg = Config(vault=vault)
    if cfg.config_path.exists():
        try:
            cfg.data = json.loads(cfg.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"[mb] broken config {cfg.config_path}: {exc}", file=sys.stderr)
            sys.exit(2)
        if not isinstance(cfg.data, dict):
            print(f"[mb] config must be a JSON object: {cfg.config_path}", file=sys.stderr)
            sys.exit(2)
        result = validate_config(cfg.data)
        if result["errors"]:
            print(f"[mb] invalid config {cfg.config_path}:", file=sys.stderr)
            for e in result["errors"]:
                print(f"[mb]   - {e}", file=sys.stderr)
            print("[mb] fix the values above or run 'mb config validate'", file=sys.stderr)
            sys.exit(2)
        migrated, changed = migrate_config(cfg.data)
        if changed:
            _backup_config(cfg.config_path)   # keep a copy before rewriting
            cfg.data = migrated
            cfg.save()
        else:
            cfg.data = migrated
    else:
        cfg.save()
    cfg.apply_env()
    return cfg


def _backup_config(config_path: Path) -> Optional[Path]:
    """Copy config aside before a migration rewrites it (never overwrite an
    existing backup)."""
    import datetime
    import shutil

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.bak-{ts}")
    suffix = 1
    while backup.exists():
        backup = config_path.with_name(f"{config_path.name}.bak-{ts}-{suffix}")
        suffix += 1
    try:
        shutil.copy2(config_path, backup)
        return backup
    except OSError:
        return None


def write_pointer(vault: Path) -> None:
    POINTER_FILE.write_text(
        json.dumps({"vault_path": str(vault)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
