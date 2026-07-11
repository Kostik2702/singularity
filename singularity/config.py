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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

DEFAULT_VAULT_WINDOWS = r"D:\obsidian\memory brain"
POINTER_FILE = Path.home() / ".singularity.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "min_score": 0.30,
    "daemon_port": 0,          # 0 = OS picks a free port; actual port -> .mb/daemon.port
    "excerpt_chars": 150,
    "batch_size": 64,
    "code_repos_dir": None,     # optional code index root
    "auto_capture": True,
    "connectors": {},
    "skip_dirs": [".obsidian", ".mb", ".git", ".trash", "templates"],
    "skip_path_parts": ["learnings/archive"],
}


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
    def daemon_port_file(self) -> Path:
        return self.mb_dir / "daemon.port"

    @property
    def daemon_pid_file(self) -> Path:
        return self.mb_dir / "daemon.pid"

    @property
    def daemon_log_file(self) -> Path:
        return self.mb_dir / "daemon.log"

    @property
    def daemon_state_file(self) -> Path:
        # atomic single-file daemon identity: {instance_id, pid, port,
        # create_time, python, started_at}
        return self.mb_dir / "daemon.json"

    @property
    def daemon_start_lock(self) -> Path:
        return self.mb_dir / "daemon.start.lock"

    @property
    def markers_dir(self) -> Path:
        return self.mb_dir / "markers"

    @property
    def lock_dir(self) -> Path:
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
    else:
        cfg.save()
    cfg.apply_env()
    return cfg


def write_pointer(vault: Path) -> None:
    POINTER_FILE.write_text(
        json.dumps({"vault_path": str(vault)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
