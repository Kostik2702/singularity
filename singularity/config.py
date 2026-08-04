"""Configuration: vault discovery + .mb/config.json.

Vault resolution order (first hit wins):
  1. explicit --vault CLI argument
  2. env MB_HOME
  3. pointer file ~/.singularity.json  -> {"vault_path": "..."}
  4. built-in default candidates (`default_vault_candidates`, overridable with
     env MB_DEFAULT_VAULT) — each tried ONLY if that path exists on this machine
     (github#1: on macOS the bare Windows default leaked into the error
     message; with nothing configured resolve_vault now returns None and
     callers show actionable guidance instead)

Runtime state lives inside the vault under `.mb/` so the vault is
self-contained and portable across machines.

EXCEPTION (WIKI-LOOP D1, 2026-07-28): `.mb/sources/` + `.mb/sources.json` hold
ingested raw documents and are the ONLY data under `.mb/` that a rebuild
cannot reconstruct. "Delete .mb and re-index" destroys them irreversibly.
They are covered by `mb backup` (with checksums), guarded by `mb doctor`
(FAIL when the registry is non-empty but the directory is gone), and can be
evacuated with `mb ingest export <dir>`.
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

# Last-resort vault location, tried only when --vault, MB_HOME and the pointer
# file all came up empty, and only if the path actually exists.
#
# This literal is one machine's layout, which is fine for a LAST resort but not
# fine as the only one: `MB_DEFAULT_VAULT` overrides the whole candidate list
# (os.pathsep-separated), and two home-relative locations are tried before it so
# a fresh install on someone else's machine has a sane default at all. The
# hardcode stays as the final fallback rather than being deleted, because it is
# what the dev machine has resolved through for the project's whole history.
DEFAULT_VAULT_WINDOWS = r"D:\obsidian\memory brain"
DEFAULT_VAULT_DIRNAME = "memory brain"
POINTER_FILE = Path.home() / ".singularity.json"


def default_vault_candidates() -> list[Path]:
    """Ordered built-in guesses for the vault, most portable first.

    `MB_DEFAULT_VAULT` replaces the list entirely (unlike `MB_HOME`, which
    short-circuits resolution above this step): it configures where to LOOK,
    not which vault to use, so a machine with several candidate layouts can
    state its own order without editing the source."""
    env = os.environ.get("MB_DEFAULT_VAULT")
    if env:
        return [Path(p.strip()).expanduser() for p in env.split(os.pathsep)
                if p.strip()]
    home = Path.home()
    return [home / DEFAULT_VAULT_DIRNAME,
            home / "Documents" / DEFAULT_VAULT_DIRNAME,
            Path(DEFAULT_VAULT_WINDOWS)]

SCHEMA_VERSION = 2   # v2 (WIKI-LOOP W10): ingest/gap/lint keys + immutable_roots

DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "model": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "min_score": 0.30,
    "daemon_port": 0,          # 0 = OS picks a free port; actual port -> .mb/daemon.port
    "excerpt_chars": 150,
    "batch_size": 64,
    "encoder_device": None,     # None = probe (cuda if a kernel actually launches, else cpu)
    "code_repos_dir": None,     # optional code index root
    "auto_capture": True,
    "brain_max_lines": 40,             # T06: per-project brain note truncation
    "session_context_max_lines": 200,  # T06: total SessionStart injection budget
    "module_note_max_lines": 60,       # T07: per-module context note truncation
    "module_context_enabled": False,   # T07: PreToolUse hook installed but no-op until true
    # T08: PreToolUse guard. On by default since 2026-08-04 (owner decision) —
    # `audit` only annotates a write, and the failure it catches is silent by
    # construction: a new note written with a client's Write tool skips the
    # template, so `source:` and the required sections are simply absent and
    # nobody notices for weeks. Existing installs are unaffected: `Config.save`
    # materializes every default into config.json, so a machine that has ever
    # saved its config already carries an explicit `false` and keeps it.
    "vault_guard_enabled": True,
    "vault_guard_mode": "audit",       # T08: "audit" (warn only) or "deny" (block)
    "personal_roots": [],              # T08: vault-relative roots classified as personal
    "digest_score_threshold": 0.45,    # T11: min cosine to surface a merge candidate
    "digest_top_k": 3,                 # T11: max candidate targets per learning section
    "nightly_schedule_time": "03:00",       # T12: mb nightly daily trigger (HH:MM)
    "nightly_nag_rate_limit_hours": 72,     # T12: min hours between SessionStart digest nags
    "markers_retention_days": 30,           # T12: nightly sweep age for .mb/markers/*
    "nightly_trend_retention_days": 180,    # T12: nightly-history.jsonl row retention
    "reflect_window_days": 30,              # T14: default `mb reflect` lookback window
    "reflect_retention_days": 90,           # T14: reflect-history.jsonl row retention (Open Decision)
    # --- WIKI-LOOP (W01-W12): raw source layer, ingest fan-out, intel lint ---
    "ingest_index_sources": True,       # D2 kill-switch for the .mb/sources_index/ corpus
    "ingest_max_source_chars": 400_000,  # W01: oversize source is REFUSED, never truncated
    "ingest_fanout_top_k": 15,          # D7: ceiling on `mb ingest plan` suggestions
    "ingest_fanout_threshold": 0.45,    # W05: min cosine, symmetric to digest_score_threshold
    "ingest_log_retention_days": 365,   # D3: .mb/ingest-log.jsonl row retention
    "immutable_roots": [".mb/sources"],  # D6: guard denies Write/Edit here even in audit mode
    "gap_link_min_inbound": 2,          # W08: inbound links before a broken link is a missing page
    "gap_learning_age_days": 30,        # W08: age at which an un-digested learning is a gap
    "lint_llm_adjudication": False,     # W09: optional batch adjudication via `mb llm`
    "backup_max_sources_mb": 200,       # W10.4: warn threshold before backing raw sources up
    # --- CODE-INDEX (C00-C07): the code corpus. `code_repos_dir` above is the
    # on/off switch (D1); this is the only other key the epic introduces.
    "code_index_max_age_days": 7,       # C05: `mb doctor` WARNs past this (no watcher, D4)
    "code_skip_dirs": [],               # C09: extra directory names pruned at any depth
    # C10: directory-name regex for vendored distributions. null = the built-in
    # `code_chunking.VENDORED_DIR_PATTERN`; "" disables the rule entirely. It is
    # configurable because the rule can over-match a first-party directory
    # (`schema-1.0.0`, `proto-3.21.12`) and `code_skip_dirs` can only ADD
    # prunes — without this key such a directory had no remedy but a source edit.
    "code_vendored_pattern": None,
    "connectors": {},
    "skip_dirs": [".obsidian", ".mb", ".git", ".trash", "templates"],
    "skip_path_parts": ["learnings/archive"],
}

# keys that carry machine-specific absolute paths (validated as strings only —
# their existence is a per-machine concern, not a config-schema error)
_PATH_KEYS = {"code_repos_dir", "hf_cache"}

# T08: config.example.json is a sanitized, git-trackable snapshot for
# onboarding on a new machine or sharing the project registry with a
# collaborator — distinct from `mb backup` (an operational, checksummed
# snapshot of the CURRENT machine's config + index pointer for disaster
# recovery on the SAME machine). hf_cache is the only currently known
# sensitive value (a local model-cache path with no portable meaning);
# actual secrets never live in config.json at all (singularity.platform.
# secrets routes them through the OS keyring / an explicit opt-in file).
_EXAMPLE_REDACTED_KEYS = {"hf_cache"}
_EXAMPLE_HOME_RELATIVE_KEYS = {"code_repos_dir"}


def _home_relative(path_str: str, home: Path) -> str:
    """`C:/Users/x/code/p` -> `~/code/p` when under home; any other path
    (outside home, or unresolvable) is retained verbatim. Never raises."""
    try:
        rel = Path(path_str).resolve().relative_to(home.resolve())
    except (OSError, ValueError):
        return path_str
    return "~/" + str(rel).replace("\\", "/")


class ConfigKeyError(ValueError):
    """A dotted key that cannot be resolved against the config's actual shape."""


_MISSING = object()
_INDEX_SUFFIX = re.compile(r"\[(\d+)\]$")


def parse_config_key(key: str) -> list[str | int]:
    """`llm.models.q.args[14]` -> `['llm', 'models', 'q', 'args', 14]`.

    The notation is not chosen here — it is the one `mb doctor` already prints
    in `A6-config-paths` (and `config_paths.path_entries` produces), so `mb
    config set` has to speak it or the printed fix stays a dead end (#14)."""
    path: list[str | int] = []
    for part in key.split("."):
        name, indexes = part, []
        while (m := _INDEX_SUFFIX.search(name)):
            indexes.insert(0, int(m.group(1)))
            name = name[: m.start()]
        if not name:
            raise ConfigKeyError(f"{key}: empty path segment")
        path.append(name)
        path.extend(indexes)
    return path


def _walk(data: Any, path: list[str | int]) -> Any:
    node: Any = data
    for step in path:
        if isinstance(step, int):
            if not isinstance(node, list) or not (0 <= step < len(node)):
                return _MISSING
            node = node[step]
        else:
            if not isinstance(node, dict) or step not in node:
                return _MISSING
            node = node[step]
    return node


def get_config_key(data: dict[str, Any], key: str) -> Any:
    """Value at a flat OR dotted key; `_MISSING` when the path does not exist.

    A literal top-level key always wins, so a config that already carries a
    key containing a dot keeps answering for it."""
    if key in data:
        return data[key]
    if "." not in key and "[" not in key:
        return _MISSING
    try:
        return _walk(data, parse_config_key(key))
    except ConfigKeyError:
        return _MISSING


def set_config_key(data: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    """A copy of `data` with `key` set, resolving dotted keys into the nested
    shape instead of creating a literal top-level key (the trap behind #14 —
    `mb config set projects.x.path` reported success three times over while the
    real value never moved).

    Intermediate nodes are never invented. Registering something new is the job
    of the command that knows the shape (`mb project add`, `mb llm add`); this
    one only rewrites values that already exist, and says so when they do not."""
    import copy

    if key in data or ("." not in key and "[" not in key):
        return {**data, key: value}

    path = parse_config_key(key)
    out = copy.deepcopy(data)
    node: Any = out
    for i, step in enumerate(path[:-1]):
        walked = ".".join(str(s) for s in path[: i + 1])
        if isinstance(step, int):
            if not isinstance(node, list) or not (0 <= step < len(node)):
                raise ConfigKeyError(f"{key}: no element at index {step} in {walked}")
        elif not isinstance(node, dict) or step not in node:
            raise ConfigKeyError(
                f"{key}: no such config section `{walked}` — this command "
                f"rewrites existing values and does not create new sections "
                f"(register a project with `mb project add`)")
        node = node[step]

    last = path[-1]
    parent = ".".join(str(s) for s in path[:-1])
    if isinstance(last, int):
        if not isinstance(node, list) or not (0 <= last < len(node)):
            raise ConfigKeyError(f"{key}: no element at index {last} in {parent}")
    else:
        if not isinstance(node, dict):
            raise ConfigKeyError(f"{key}: `{parent}` is not a section")
        if last not in node:
            raise ConfigKeyError(
                f"{key}: `{parent}` has no key `{last}` (present: "
                f"{', '.join(sorted(map(str, node))) or '(none)'})")
    node[last] = value
    return out


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
    _int("module_note_max_lines", 1, 10_000)
    _num("digest_score_threshold", 0.0, 1.0)
    _int("digest_top_k", 1, 50)
    _int("nightly_nag_rate_limit_hours", 1, 24 * 30)
    _int("markers_retention_days", 1, 3650)
    _int("nightly_trend_retention_days", 1, 3650)
    _int("reflect_window_days", 1, 3650)
    _int("reflect_retention_days", 1, 3650)
    # WIKI-LOOP
    _int("ingest_max_source_chars", 1, 10_000_000)
    _int("ingest_fanout_top_k", 1, 100)
    _num("ingest_fanout_threshold", 0.0, 1.0)
    _int("ingest_log_retention_days", 1, 3650)
    _int("gap_link_min_inbound", 1, 100)
    _int("gap_learning_age_days", 1, 3650)
    _int("backup_max_sources_mb", 1, 100_000)
    # CODE-INDEX
    _int("code_index_max_age_days", 1, 3650)
    if "code_vendored_pattern" in data and data["code_vendored_pattern"] is not None:
        value = data["code_vendored_pattern"]
        if not isinstance(value, str):
            errors.append("code_vendored_pattern: must be a regex string, "
                          '"" to disable the rule, or null for the built-in')
        else:
            try:
                re.compile(value)
            except re.error as exc:
                errors.append(f"code_vendored_pattern: invalid regex ({exc})")
    for key in ("ingest_index_sources", "lint_llm_adjudication"):
        if key in data and not isinstance(data[key], bool):
            errors.append(f"{key}: must be a boolean")
    if "immutable_roots" in data and (not isinstance(data["immutable_roots"], list)
                                      or not all(isinstance(x, str) for x in data["immutable_roots"])):
        errors.append("immutable_roots: must be a list of strings")
    if "nightly_schedule_time" in data:
        v = data["nightly_schedule_time"]
        if not (isinstance(v, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v)):
            errors.append("nightly_schedule_time: must be 'HH:MM' 24h string")
    for key in ("skip_dirs", "skip_path_parts", "code_skip_dirs"):
        if key in data and (not isinstance(data[key], list)
                            or not all(isinstance(x, str) for x in data[key])):
            errors.append(f"{key}: must be a list of strings")
    if "auto_capture" in data and not isinstance(data["auto_capture"], bool):
        errors.append("auto_capture: must be a boolean")
    if "module_context_enabled" in data and not isinstance(data["module_context_enabled"], bool):
        errors.append("module_context_enabled: must be a boolean")
    if "vault_guard_enabled" in data and not isinstance(data["vault_guard_enabled"], bool):
        errors.append("vault_guard_enabled: must be a boolean")
    if "vault_guard_mode" in data and data["vault_guard_mode"] not in ("audit", "deny"):
        errors.append("vault_guard_mode: must be 'audit' or 'deny'")
    if "personal_roots" in data and (not isinstance(data["personal_roots"], list)
                                     or not all(isinstance(x, str) for x in data["personal_roots"])):
        errors.append("personal_roots: must be a list of strings")
    if "connectors" in data and not isinstance(data["connectors"], dict):
        errors.append("connectors: must be an object")
    if ("encoder_device" in data and data["encoder_device"] is not None
            and not isinstance(data["encoder_device"], str)):
        errors.append("encoder_device: must be a device string (e.g. 'cpu', 'cuda') or null")
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
    # v1 -> v2 (WIKI-LOOP W10) needs no value rewriting: every new key is
    # additive and resolves through DEFAULT_CONFIG, so a v1 config keeps every
    # user value and gains the defaults. The bump exists so `mb doctor` and
    # backups can tell the generations apart; `load_config` takes a timestamped
    # .bak via _backup_config before persisting it.
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
    def code_repos_dir(self) -> "Path | None":
        """Root holding the repositories the code corpus indexes, or None when
        the corpus is switched off.

        CODE-INDEX D1: an unset key IS the off switch — a separate boolean
        would be a second source of truth that can disagree with this one.
        `~` is expanded because `config.example.json` deliberately stores this
        key home-relative for portability (`_EXAMPLE_HOME_RELATIVE_KEYS`), so a
        config seeded from that example arrives as `~/code`."""
        raw = self.data.get("code_repos_dir")
        if raw is None or not str(raw).strip():
            return None
        return Path(str(raw).strip()).expanduser()

    @property
    def sources_dir(self) -> Path:
        # WIKI-LOOP D1: immutable raw documents, deliberately OUTSIDE the vault
        # note tree so Obsidian/curator/digest never see them. The only
        # non-reconstructible data under .mb/ — see this module's docstring.
        return self.mb_dir / "sources"

    @property
    def sources_index_dir(self) -> Path:
        # WIKI-LOOP D2: second search corpus over sources_dir, physically
        # separate from memory_index so wiki analytics cannot be polluted.
        return self.mb_dir / "sources_index"

    @property
    def sources_registry_path(self) -> Path:
        return self.mb_dir / "sources.json"

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

    def corpus_lock_dir(self, corpus: str) -> Path:
        """Per-corpus writer lock (W03). The memory corpus keeps the historic
        `rebuild.lock` path verbatim so an in-flight lock from an older build
        is still honored; every other corpus gets its own, so indexing sources
        never blocks indexing the vault (and vice versa)."""
        if corpus == "memory":
            return self.lock_dir
        return self.mb_dir / f"rebuild-{corpus}.lock"

    @property
    def ingest_lock_dir(self) -> Path:
        """Serializes registry/journal mutations (`mb ingest add|mark`) so two
        concurrent writers cannot lose an entry (W01/W06)."""
        return self.mb_dir / "ingest.lock"

    # --- config values --------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        if key in self.data:
            return self.data[key]
        if key in DEFAULT_CONFIG:
            return DEFAULT_CONFIG[key]
        # Dotted read-back, so `mb config get` and the daemon's `config` command
        # answer for exactly the keys `mb doctor` names and `mb config set`
        # writes. Only reached when no literal key matched, so flat lookups
        # keep their previous behavior.
        found = get_config_key(self.data, key)
        return default if found is _MISSING else found

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
        try:
            self._write_sanitized_example(merged)
        except Exception:  # noqa: BLE001 — example generation must never break a real save
            pass

    @property
    def config_example_path(self) -> Path:
        return self.mb_dir / "config.example.json"

    def _write_sanitized_example(self, merged: dict[str, Any]) -> None:
        """T08: git-trackable sanitized snapshot of the config. No secrets
        (hf_cache redacted), project/code paths under home rewritten
        `~/...` for portability, paths outside home retained as-is."""
        sanitized = {k: v for k, v in merged.items() if k not in _EXAMPLE_REDACTED_KEYS}
        home = Path.home()
        for key in _EXAMPLE_HOME_RELATIVE_KEYS:
            if isinstance(sanitized.get(key), str):
                sanitized[key] = _home_relative(sanitized[key], home)
        if isinstance(sanitized.get("projects"), dict):
            sanitized["projects"] = {
                pid: ({**entry, "path": _home_relative(entry["path"], home)}
                     if isinstance(entry, dict) and isinstance(entry.get("path"), str)
                     else entry)
                for pid, entry in sanitized["projects"].items()
            }

        # Bootstrap-clobber guard: a fresh `mb init`/empty-projects save must
        # never overwrite an existing, richer example (e.g. pulled via git
        # from another machine that already registered projects) with a
        # near-empty one.
        is_bootstrap = not self.data.get("projects")
        if is_bootstrap and self.config_example_path.exists():
            try:
                existing = json.loads(self.config_example_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
            if isinstance(existing, dict) and existing.get("projects"):
                return

        tmp = self.config_example_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(sanitized, indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        tmp.replace(self.config_example_path)

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


def atomic_write_json(path: Path, data: Any, *, indent: int = 1) -> None:
    """Shared tmp+replace idiom for every JSON snapshot/journal the engine
    writes outside Config.save() itself (digest journal, nightly snapshots,
    reflect snapshot/history) -- same convention, factored out so a fourth
    independent copy of it is never written again."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), encoding="utf-8")
    tmp.replace(path)


def read_config_tolerant(vault: Path) -> dict[str, Any]:
    """Dependency-free, never-raising read of vault/.mb/config.json — for hook
    hot paths (SessionStart, PreToolUse, ...) that must NOT go through the
    full load_config() gate: that path sys.exit(2)s on a malformed config,
    which is correct CLI behavior but would kill a hook mid-session. Missing/
    malformed/non-object -> {}; every caller already treats absence as "use
    the default". See [[Rule - hooks must never use the full validated Config
    load path]]."""
    cfg_path = vault / ".mb" / "config.json"
    if not cfg_path.exists():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def config_unreadable(vault: Path) -> bool:
    """True when `.mb/config.json` EXISTS but cannot be parsed as an object.

    `read_config_tolerant` flattens "absent", "malformed" and "not an object"
    into `{}`, which is right for readers that only want values — but a caller
    deciding whether a default APPLIES needs the difference. An absent key in a
    readable config means "the shipped default"; an unparseable file means the
    user's choice is unknown, and a hook that acts on a guess there would be
    enforcing a setting nobody could have expressed."""
    cfg_path = vault / ".mb" / "config.json"
    if not cfg_path.exists():
        return False
    try:
        return not isinstance(json.loads(cfg_path.read_text(encoding="utf-8-sig")), dict)
    except (OSError, json.JSONDecodeError):
        return True


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
    for candidate in default_vault_candidates():
        # `.exists()` is not merely False on a path we may not stat: macOS TCC
        # raises EPERM for `~/Documents` until the terminal is granted access,
        # which would surface as a traceback instead of the actionable
        # "no vault configured" message this function exists to enable.
        try:
            if candidate.exists():
                return candidate
        except OSError:
            continue
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
