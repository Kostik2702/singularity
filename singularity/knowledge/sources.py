"""Raw source layer (WIKI-LOOP W01): content-addressed registry + write-once
storage for ingested external documents.

Two artifacts, both under `.mb/` (decision D1 — raw never enters the vault
note tree, so Obsidian's graph and every consumer of `memory_index`
(`curator`, `digest`) stay clean physically rather than by convention):

  .mb/sources.json                              registry (atomic tmp+replace)
  .mb/sources/<YYYY>/<slug>--<id8>.md           raw copy, frontmatter + verbatim body

Invariants this module owns:

* **Content-addressed id.** `source_id = sha256(normalize(text))[:12]` where
  `normalize` is `capture.normalize_gist` — the SAME normalization the capture
  de-duplicator uses, so "the same text with different whitespace" resolves to
  one source instead of two.
* **Write-once (D6).** An existing `stored_path` is NEVER rewritten — not on a
  repeated add, not on a hash mismatch. This is the layer that actually
  guarantees immutability; the PreToolUse guard (W04) is a second line that is
  a no-op while `vault_guard_enabled` is false.
* **Tolerant read, strict write.** A missing/corrupt registry never crashes a
  read path (same contract as `digest._read_journal`), but a mutation refuses
  to run against an unreadable registry rather than silently clobbering it.
* **Writes only under `.mb/`** (D5). This module is the only one of the
  WIKI-LOOP additions that writes at all, and it never touches a vault note.
  Enforced by `tests/test_wiki_loop_report_only.py`.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from .capture import normalize_gist
from ..config import Config, atomic_write_json
from ..platform.fslock import make_lock_dir

REGISTRY_SCHEMA_VERSION = 1
LOG_NAME = "ingest-log.jsonl"   # D3: machine history, sibling of nightly-history.jsonl
SOURCE_ID_CHARS = 12          # 48 bits of sha256 — collision-safe at vault scale
STORED_ID_CHARS = 8           # filename discriminator
SLUG_MAX_CHARS = 60
DEFAULT_CONTENT_TYPE = "text/markdown"
STATUSES = ("new", "partial", "processed")
ORIGIN_KINDS = ("file", "stdin", "connector", "external")

# lock: same cross-process mkdir idiom as indexer._writer_lock, with the same
# rule — a contended acquisition NEVER removes the other writer's lock.
STALE_LOCK_SECONDS = 6 * 3600
_LOCK_WAIT_SECONDS = 15.0
_LOCK_POLL_SECONDS = 0.05

_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WS_RE = re.compile(r"\s+")
_DASH_RUN_RE = re.compile(r"-{2,}")
_H1_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
# Windows device names: reserved as a whole basename, with or without extension
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL",
                 *(f"COM{i}" for i in range(1, 10)),
                 *(f"LPT{i}" for i in range(1, 10))}


class SourceError(RuntimeError):
    """Actionable ingest failure — the message is meant to be printed verbatim.

    Maps to CLI exit 2 (bad input / config violation)."""


class SourceStateError(SourceError):
    """On-disk state problem (corrupt registry, contended lock) rather than bad
    input. Maps to CLI exit 1 so scripts can tell "fix your command" apart from
    "the store is busy/damaged"."""


class RegistryLockedError(SourceStateError):
    """Another `mb ingest` writer holds the registry lock."""


# ---------------------------------------------------------------------------
# identity + naming
# ---------------------------------------------------------------------------

def compute_source_id(text: str) -> str:
    """Content-addressed id over the NORMALIZED text (whitespace/punctuation
    insensitive). Falls back to the raw text when normalization empties the
    document (e.g. a file of nothing but punctuation) — otherwise every such
    document would collapse onto one id."""
    basis = normalize_gist(text) or text
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:SOURCE_ID_CHARS]


def content_sha256(text: str) -> str:
    """Hash of the RAW text — distinguishes versions that normalization would
    collapse, and is what `supersedes` detection compares."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slugify(title: str, max_chars: int = SLUG_MAX_CHARS) -> str:
    """Windows-safe filename stem. Handles: forbidden characters, control
    characters, reserved device names, trailing dots/spaces (silently dropped
    by the Win32 layer, which would desynchronize the registry from disk),
    length, and NFC normalization. Cyrillic and emoji are legal on NTFS and
    are kept — the vault is already non-ASCII."""
    text = unicodedata.normalize("NFC", str(title or ""))
    text = _FORBIDDEN_RE.sub("-", text)
    text = _WS_RE.sub("-", text)
    text = _DASH_RUN_RE.sub("-", text)
    text = text.strip(" .-")
    if len(text) > max_chars:
        text = text[:max_chars].strip(" .-")
    if not text:
        return "source"
    if text.split(".", 1)[0].upper() in _WIN_RESERVED:
        # unreachable with the current `<slug>--<id8>` scheme (the suffix
        # already breaks the device name), kept so a future rename of the
        # scheme cannot silently reintroduce CON.md
        text = "_" + text
    return text


def _derive_title(text: str, origin_ref: str) -> str:
    m = _H1_RE.search(text)
    if m:
        return m.group(1).strip()
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if stripped and not stripped.startswith("---"):
            return stripped[:200]
    if origin_ref:
        return Path(origin_ref).name or origin_ref
    return "source"


def is_url(value: str) -> bool:
    return bool(_URL_RE.match(str(value or "").strip()))


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def registry_path(cfg: Config) -> Path:
    return cfg.sources_registry_path


def _empty_registry() -> dict[str, Any]:
    return {"schema_version": REGISTRY_SCHEMA_VERSION, "sources": {}}


def _read_registry_raw(cfg: Config) -> tuple[dict[str, Any], bool]:
    """(registry, readable). `readable` is False only when the file EXISTS but
    cannot be parsed into the expected shape — the signal a mutation needs to
    refuse instead of overwriting a damaged file."""
    path = registry_path(cfg)
    if not path.exists():
        return _empty_registry(), True
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return _empty_registry(), False
    if not isinstance(data, dict) or not isinstance(data.get("sources"), dict):
        return _empty_registry(), False
    data.setdefault("schema_version", REGISTRY_SCHEMA_VERSION)
    return data, True


def read_registry(cfg: Config) -> dict[str, Any]:
    """Tolerant read for report paths: a corrupt registry reads as empty and
    never raises (same contract as `digest._read_journal`, digest.py:36)."""
    return _read_registry_raw(cfg)[0]


def _read_registry_strict(cfg: Config) -> dict[str, Any]:
    data, ok = _read_registry_raw(cfg)
    if not ok:
        raise SourceStateError(
            f"registry is unreadable: {registry_path(cfg)}\n"
            f"refusing to overwrite it. Inspect or move the file aside "
            f"(a fresh empty registry is created automatically), then retry. "
            f"Raw documents under {cfg.sources_dir} are untouched.")
    return data


def write_registry(cfg: Config, data: dict[str, Any]) -> None:
    data["schema_version"] = REGISTRY_SCHEMA_VERSION
    atomic_write_json(registry_path(cfg), data)


@contextlib.contextmanager
def registry_lock(cfg: Config, wait_seconds: float = _LOCK_WAIT_SECONDS):
    """Serialize registry/journal mutations across processes.

    Same mkdir idiom and stale-takeover rule as `indexer._writer_lock`
    (indexer.py:153): a contended acquisition NEVER removes the holder's lock,
    and only a lock older than STALE_LOCK_SECONDS is taken over. Unlike the
    index lock this one briefly WAITS (registry writes are milliseconds, so a
    hard failure on contention would be user-hostile)."""
    lock = cfg.ingest_lock_dir
    lock.parent.mkdir(parents=True, exist_ok=True)
    owns = False
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        if make_lock_dir(lock):              # False also while its holder is releasing it
            owns = True
            break
        try:
            age = time.time() - lock.stat().st_mtime
        except OSError:
            age = 0.0
        if age >= STALE_LOCK_SECONDS:
            owns = True              # stale -> take over; we now own cleanup
            break
        if time.monotonic() >= deadline:
            raise RegistryLockedError(
                "another `mb ingest` writer holds the registry lock "
                f"({lock}); retry shortly, or remove the directory if no "
                "ingest is running")
        time.sleep(_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        if owns:
            shutil.rmtree(lock, ignore_errors=True)


# ---------------------------------------------------------------------------
# stored raw file
# ---------------------------------------------------------------------------

def stored_relpath(source_id: str, title: str, ingested_at: str) -> str:
    """Vault-relative, forward-slash path of the raw copy. Vault-relative (not
    absolute) so the registry survives moving the vault to another machine."""
    year = (ingested_at or "")[:4] or datetime.now().strftime("%Y")
    return f".mb/sources/{year}/{slugify(title)}--{source_id[:STORED_ID_CHARS]}.md"


def stored_abspath(cfg: Config, rel: str) -> Path:
    return cfg.resolve(rel)


def _yaml_scalar(value: Any) -> str:
    """JSON encoding is a valid YAML 1.2 double-quoted scalar, so this quotes
    and escapes in one step. Quoting is NOT cosmetic: an all-digit source_id or
    sha256 prefix would otherwise be parsed back as an integer."""
    return json.dumps(value, ensure_ascii=False)


def render_source_file(record: dict[str, Any], text: str) -> str:
    """Wrapper frontmatter + the raw body VERBATIM (no markdown cleanup, no
    normalization) — a source must be reproducible byte-for-byte."""
    origin = record.get("origin") or {}
    lines = [
        "---",
        "type: source",
        f"source_id: {_yaml_scalar(record['source_id'])}",
        f"title: {_yaml_scalar(record.get('title', ''))}",
        "origin: {{kind: {}, ref: {}}}".format(_yaml_scalar(origin.get("kind", "file")),
                                         _yaml_scalar(origin.get("ref", ""))),
        f"sha256: {_yaml_scalar(record['sha256'])}",
        f"bytes: {int(record.get('bytes', 0))}",
        f"chars: {int(record.get('chars', 0))}",
        f"content_type: {_yaml_scalar(record.get('content_type', DEFAULT_CONTENT_TYPE))}",
        f"ingested_at: {_yaml_scalar(record.get('ingested_at', ''))}",
        "immutable: true",
    ]
    if record.get("project"):
        lines.append(f"project: {_yaml_scalar(record['project'])}")
    if record.get("tags"):
        lines.append("tags: [" + ", ".join(_yaml_scalar(t) for t in record["tags"]) + "]")
    lines += ["---", ""]
    return "\n".join(lines) + text


def read_source_text(cfg: Config, source_id: str) -> str | None:
    """The stored raw body (wrapper frontmatter stripped), or None."""
    record = get(cfg, source_id)
    if not record or not record.get("stored") or not record.get("stored_path"):
        return None
    path = stored_abspath(cfg, record["stored_path"])
    if not path.is_file():
        return None
    from ..search.chunking import parse_frontmatter
    # newline="" — no universal-newline translation, so a source stored with
    # CRLF round-trips byte-for-byte (Path.read_text gained `newline` only in
    # 3.13; this project supports 3.10+).
    with open(path, encoding="utf-8", errors="ignore", newline="") as fh:
        raw = fh.read()
    _, body = parse_frontmatter(raw)
    return body[1:] if body.startswith("\n") else body


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------

def get(cfg: Config, source_id: str) -> dict[str, Any] | None:
    record = read_registry(cfg)["sources"].get(str(source_id))
    return dict(record, source_id=str(source_id)) if isinstance(record, dict) else None


def list_sources(cfg: Config, status: str | None = None,
                 limit: int | None = None) -> list[dict[str, Any]]:
    """Newest first, stable tiebreak on id so the order never depends on dict
    insertion order (a repeated run must be byte-identical)."""
    rows = [dict(rec, source_id=sid)
            for sid, rec in read_registry(cfg)["sources"].items()
            if isinstance(rec, dict) and (status is None or rec.get("status") == status)]
    rows.sort(key=lambda r: (r.get("ingested_at") or "", r["source_id"]), reverse=True)
    return rows[:limit] if limit else rows


def _find_superseded(sources: dict[str, Any], origin_ref: str,
                     sha256: str) -> str | None:
    """Newest live record with the SAME origin.ref but different content — the
    one this add supersedes. Records already superseded are skipped so a chain
    of three versions links 1->2->3, not 1->3 and 2->3."""
    if not origin_ref:
        return None
    best_id, best_ts = None, ""
    for sid, rec in sources.items():
        if not isinstance(rec, dict) or rec.get("superseded_by"):
            continue
        if (rec.get("origin") or {}).get("ref") != origin_ref:
            continue
        if rec.get("sha256") == sha256:
            continue
        ts = str(rec.get("ingested_at") or "")
        if best_id is None or (ts, sid) > (best_ts, best_id):
            best_id, best_ts = sid, ts
    return best_id


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

def add(cfg: Config, text: str, *, title: str | None = None,
        origin_kind: str = "file", origin_ref: str = "",
        project: str | None = None, tags: list[str] | None = None,
        content_type: str = DEFAULT_CONTENT_TYPE,
        dry_run: bool = False, now: str | None = None) -> dict[str, Any]:
    """Register + store one raw document. Idempotent: the same content yields
    `duplicate: True` and touches neither disk nor registry.

    Ordering is deliberate — the raw file is written BEFORE the registry. A
    crash in between leaves an orphan raw file (a `mb doctor` WARN, fully
    recoverable) instead of a registry entry pointing at nothing (a FAIL)."""
    if not isinstance(text, str):
        raise SourceError("source content must be text")
    if not text.strip():
        raise SourceError("empty source: nothing to ingest")
    if origin_kind not in ORIGIN_KINDS:
        raise SourceError(f"unknown origin kind {origin_kind!r} "
                          f"(expected one of: {', '.join(ORIGIN_KINDS)})")

    max_chars = int(cfg.get("ingest_max_source_chars"))
    if len(text) > max_chars:
        raise SourceError(
            f"source is {len(text)} chars, limit `ingest_max_source_chars` is "
            f"{max_chars}. Truncating a source silently is never done — split "
            f"the document and ingest the parts, or raise the limit with "
            f"`mb config set ingest_max_source_chars <n>`.")

    origin_ref = str(origin_ref or "")
    title = str(title).strip() if title and str(title).strip() else _derive_title(text, origin_ref)
    sid = compute_source_id(text)
    sha = content_sha256(text)
    ts = now or datetime.now().isoformat(timespec="seconds")
    rel = stored_relpath(sid, title, ts)
    abs_path = stored_abspath(cfg, rel)

    if dry_run:
        # strict read even here: a corrupt registry must surface BEFORE the
        # user believes the dry run reflects reality (it writes nothing either way)
        registry = _read_registry_strict(cfg)
        existing = registry["sources"].get(sid)
        return {"source_id": sid, "duplicate": bool(existing) or abs_path.exists(),
                "stored_path": (existing or {}).get("stored_path", rel),
                "stored": True, "title": title, "sha256": sha,
                "bytes": len(text.encode("utf-8")), "chars": len(text),
                "supersedes": _find_superseded(registry["sources"], origin_ref, sha),
                "dry_run": True}

    with registry_lock(cfg):
        registry = _read_registry_strict(cfg)
        sources = registry["sources"]

        existing = sources.get(sid)
        if isinstance(existing, dict):
            return {"source_id": sid, "duplicate": True,
                    "stored_path": existing.get("stored_path"),
                    "stored": bool(existing.get("stored")),
                    "title": existing.get("title"), "sha256": existing.get("sha256"),
                    "bytes": existing.get("bytes"), "chars": existing.get("chars"),
                    "supersedes": existing.get("supersedes"), "dry_run": False}

        superseded = _find_superseded(sources, origin_ref, sha)
        record = {
            "title": title,
            "sha256": sha,
            "bytes": len(text.encode("utf-8")),
            "chars": len(text),
            "origin": {"kind": origin_kind, "ref": origin_ref},
            "stored_path": rel,
            "stored": True,
            "content_type": content_type or DEFAULT_CONTENT_TYPE,
            "project": project,
            "tags": list(tags or []),
            "ingested_at": ts,
            "status": "new",
            "derived_notes": [],
            "marks": {},
            "supersedes": superseded,
            "superseded_by": None,
        }

        recovered = abs_path.exists()
        if not recovered:
            # write-once: the existence check above is the ONLY gate, and it is
            # inside the lock, so two concurrent adds cannot both write.
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = abs_path.with_name(abs_path.name + ".tmp")
            tmp.write_text(render_source_file(dict(record, source_id=sid), text),
                           encoding="utf-8", newline="")
            os.replace(tmp, abs_path)

        sources[sid] = record
        if superseded:
            sources[superseded]["superseded_by"] = sid
        write_registry(cfg, registry)

    return {"source_id": sid, "duplicate": recovered, "stored_path": rel,
            "stored": True, "title": title, "sha256": sha,
            "bytes": record["bytes"], "chars": record["chars"],
            "supersedes": superseded, "recovered": recovered, "dry_run": False}


def attach_notes(cfg: Config, source_id: str, notes: list[str],
                 status: str | None = None, *, actor: str = "manual",
                 op: str = "ingest", log: bool = False,
                 now: str | None = None) -> dict[str, Any]:
    """Record which vault notes were derived from a source (the provenance
    back-link; the forward link is `sources: [<id>]` in the note's own
    frontmatter).

    Tolerant by design, like `digest.mark` (digest.py:58): a note path that no
    longer exists is still accepted. The registry records what WAS derived —
    it is a history, not an index of the live vault, and refusing to record it
    because a file was later renamed would lose the fact entirely.

    Each attachment also lands in `record["marks"]` stamped with the source's
    sha256 AT THE TIME OF MARKING. `ingest_plan` suppresses an already-handled
    page only while that stamp still matches, so a re-ingested (hence
    re-hashed) document legitimately proposes the same pages again instead of
    being permanently silenced.

    `log=True` additionally appends one line to `.mb/ingest-log.jsonl` — the
    machine-readable history (D3). Kept opt-in so the provenance back-link
    written by `mb note new --from-source` does not masquerade as a completed
    ingest cycle."""
    if status is not None and status not in STATUSES:
        raise SourceError(f"unknown status {status!r} "
                          f"(expected one of: {', '.join(STATUSES)})")
    clean = [str(n).replace("\\", "/").strip() for n in (notes or [])]
    clean = [n for n in clean if n]
    stamp = now or datetime.now().isoformat(timespec="seconds")
    with registry_lock(cfg):
        registry = _read_registry_strict(cfg)
        record = registry["sources"].get(str(source_id))
        if not isinstance(record, dict):
            raise SourceError(f"unknown source_id: {source_id}")
        previous = list(record.get("derived_notes") or [])
        merged = sorted(set(previous) | set(clean))
        added = [n for n in clean if n not in previous]
        marks = dict(record.get("marks") or {})
        for note in clean:
            marks[note] = {"marked_at": stamp, "sha256": record.get("sha256")}
        record["derived_notes"] = merged
        record["marks"] = marks
        if status is not None:
            record["status"] = status
        title, final_status = record.get("title"), record.get("status")
        write_registry(cfg, registry)
        if log:
            _append_log(cfg, {"ts": stamp, "op": op, "source_id": str(source_id),
                              "title": title, "notes": clean, "status": final_status,
                              "actor": actor})
    return {"source_id": str(source_id), "derived_notes": merged,
            "added": added, "status": final_status, "logged": bool(log)}


# ---------------------------------------------------------------------------
# machine log (.mb/ingest-log.jsonl) — D3
# ---------------------------------------------------------------------------

def log_path(cfg: Config) -> Path:
    return cfg.mb_dir / LOG_NAME


def _read_log_lines(cfg: Config) -> list[str]:
    path = log_path(cfg)
    if not path.exists():
        return []
    try:
        return [ln for ln in path.read_text(encoding="utf-8-sig").splitlines() if ln.strip()]
    except OSError:
        return []


def _write_log_lines(cfg: Config, lines: list[str]) -> None:
    """Whole-file tmp+replace, same idiom as `nightly._step_trend`
    (nightly.py:147). Chosen over an O_APPEND single write in W00 §7: append
    atomicity only holds below PIPE_BUF on POSIX, Windows gives weaker
    guarantees still, and one line carrying a long `notes` list easily exceeds
    it — a torn line is far more expensive than rewriting a file that holds
    one row per ingest."""
    path = log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    tmp.replace(path)


def _append_log(cfg: Config, row: dict[str, Any]) -> None:
    """Caller MUST already hold `registry_lock` — the read-modify-write below
    is only atomic against other writers that take the same lock."""
    _write_log_lines(cfg, _read_log_lines(cfg)
                     + [json.dumps(row, ensure_ascii=False)])


def read_log(cfg: Config, since: str | None = None, op: str | None = None,
             limit: int | None = None) -> list[dict[str, Any]]:
    """Newest last (chronological, as written). Unparseable lines are skipped
    rather than raised on — a damaged history must never break a read."""
    rows: list[dict[str, Any]] = []
    for line in _read_log_lines(cfg):
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if op and row.get("op") != op:
            continue
        if since and str(row.get("ts", "")) < str(since):
            continue
        rows.append(row)
    return rows[-limit:] if limit else rows


def sweep_log(cfg: Config, now: float | None = None) -> dict[str, int]:
    """Drop rows older than `ingest_log_retention_days`. Called from
    `mb nightly` next to the markers sweep."""
    from datetime import timedelta

    days = float(cfg.get("ingest_log_retention_days"))
    reference = datetime.fromtimestamp(now) if now is not None else datetime.now()
    cutoff = (reference - timedelta(days=days)).isoformat(timespec="seconds")
    lines = _read_log_lines(cfg)
    kept: list[str] = []
    for line in lines:
        try:
            row = json.loads(line)
            ts = str(row["ts"])
        except (json.JSONDecodeError, KeyError, TypeError):
            continue          # unparseable rows are dropped, never accumulated
        if ts >= cutoff:
            kept.append(line)
    if len(kept) != len(lines):
        with registry_lock(cfg):
            _write_log_lines(cfg, kept)
    return {"kept": len(kept), "swept": len(lines) - len(kept)}


def marked_notes(record: dict[str, Any]) -> set[str]:
    """Notes already handled for THIS version of the source. A mark whose
    stamped sha no longer matches is ignored, so a re-hashed document proposes
    its pages again."""
    sha = record.get("sha256")
    marks = record.get("marks") or {}
    if not isinstance(marks, dict):
        return set()
    return {note for note, info in marks.items()
            if isinstance(info, dict) and info.get("sha256") == sha}


# ---------------------------------------------------------------------------
# backfill (W10.1): retro-provenance for notes written before the ingest layer
# ---------------------------------------------------------------------------

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
_REF_LOOKS_EXTERNAL_RE = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*://|[A-Za-z]:[\\/]|/)")
BACKFILL_SCAN_ROOTS = ("reference/",)   # bodies scanned for bare URLs
BACKFILL_MAX_URLS_PER_NOTE = 20


def external_source_id(ref: str) -> str:
    """Stable id for a source whose CONTENT we never had. Derived from the
    reference itself, so re-running backfill maps the same URL to the same
    record instead of minting a new one."""
    return hashlib.sha256(f"external:{str(ref).strip().lower()}".encode()
                          ).hexdigest()[:SOURCE_ID_CHARS]


def _looks_external(value: Any) -> bool:
    """A `source:` frontmatter value pointing at a document, not prose.

    The vault is full of `source: "brainstorm + grill, session 2026-07-03"` —
    real provenance, but not a retrievable document, and registering it would
    fill the registry with things nobody can ever ingest."""
    text = str(value or "").strip().strip('"\'')
    return bool(text) and bool(_REF_LOOKS_EXTERNAL_RE.match(text))


def _discover_external_refs(cfg: Config) -> tuple[dict[str, set[str]], int, list[str]]:
    """(ref -> notes citing it, notes_scanned, capped_notes)."""
    from ..search.chunking import parse_frontmatter, should_skip

    skip_dirs = list(cfg.get("skip_dirs") or ())
    skip_parts = list(cfg.get("skip_path_parts") or ())
    found: dict[str, set[str]] = {}
    capped: list[str] = []
    scanned = 0
    for path in sorted(cfg.vault.rglob("*.md")):
        rel = path.relative_to(cfg.vault).as_posix()
        if should_skip(rel, skip_dirs, skip_parts):
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        fm, body = parse_frontmatter(raw)
        if _looks_external(fm.get("source")):
            found.setdefault(str(fm["source"]).strip().strip('"\''), set()).add(rel)
        if rel.startswith(BACKFILL_SCAN_ROOTS):
            urls = []
            for match in _URL_IN_TEXT_RE.finditer(body):
                urls.append(match.group(0).rstrip(".,;:)»\"'"))
            unique = sorted(set(urls))
            if len(unique) > BACKFILL_MAX_URLS_PER_NOTE:
                capped.append(rel)
                unique = unique[:BACKFILL_MAX_URLS_PER_NOTE]
            for url in unique:
                found.setdefault(url, set()).add(rel)
    return found, scanned, capped


def backfill(cfg: Config, apply: bool = False,
             now: str | None = None) -> dict[str, Any]:
    """Register the external documents existing notes already cite.

    Without this, `mb curator gaps` would believe the vault has no sources at
    all — hundreds of notes derived from articles and specs would look
    self-generated. The text itself is gone (it was never stored), so these
    records are `stored: false`: a provenance fact, not a retrievable document.

    Idempotent: a second `--apply` run reports zero changes."""
    stamp = now or datetime.now().isoformat(timespec="seconds")
    discovered, scanned, capped = _discover_external_refs(cfg)

    registry, ok = _read_registry_raw(cfg)
    if not ok and apply:
        raise SourceStateError(
            f"registry is unreadable: {registry_path(cfg)}; refusing to overwrite it")
    sources_map = registry["sources"]

    created: list[dict[str, Any]] = []
    updated: list[dict[str, Any]] = []
    for ref, notes in sorted(discovered.items()):
        sid = external_source_id(ref)
        note_list = sorted(notes)
        record = sources_map.get(sid)
        if isinstance(record, dict):
            missing = [n for n in note_list if n not in (record.get("derived_notes") or [])]
            if missing:
                updated.append({"source_id": sid, "ref": ref, "added_notes": missing})
            continue
        created.append({"source_id": sid, "ref": ref, "derived_notes": note_list})

    if apply and (created or updated):
        with registry_lock(cfg):
            registry = _read_registry_strict(cfg)
            sources_map = registry["sources"]
            for entry in created:
                sha = hashlib.sha256(
                    f"external:{entry['ref'].strip().lower()}".encode()).hexdigest()
                sources_map[entry["source_id"]] = {
                    "title": entry["ref"][:200],
                    "sha256": sha, "bytes": 0, "chars": 0,
                    "origin": {"kind": "external", "ref": entry["ref"]},
                    "stored_path": None, "stored": False,
                    "content_type": None, "project": None, "tags": [],
                    "ingested_at": stamp,
                    # the notes already exist -> the cycle is closed by
                    # definition; leaving these "new" would flood `curator gaps`
                    "status": "processed",
                    "derived_notes": entry["derived_notes"],
                    "marks": {n: {"marked_at": stamp, "sha256": sha}
                              for n in entry["derived_notes"]},
                    "supersedes": None, "superseded_by": None,
                }
            for entry in updated:
                record = sources_map[entry["source_id"]]
                merged = sorted(set(record.get("derived_notes") or [])
                                | set(entry["added_notes"]))
                record["derived_notes"] = merged
                marks = dict(record.get("marks") or {})
                for note in entry["added_notes"]:
                    marks[note] = {"marked_at": stamp, "sha256": record.get("sha256")}
                record["marks"] = marks
            write_registry(cfg, registry)

    return {"applied": bool(apply), "notes_scanned": scanned,
            "discovered": len(discovered), "created": len(created),
            "updated": len(updated), "capped_notes": capped,
            "entries": created + updated}


# ---------------------------------------------------------------------------
# export (D1 escape hatch: raw lives under .mb/, which people delete)
# ---------------------------------------------------------------------------

def export(cfg: Config, dest: Path, source_id: str | None = None,
           force: bool = False) -> dict[str, Any]:
    """COPY the registry + raw documents out of `.mb/` so "nuke .mb and
    re-index" stops being destructive. Never moves, never deletes."""
    dest = Path(dest)
    if dest.exists() and any(dest.iterdir()) and not force:
        raise SourceError(f"destination is not empty: {dest}\n"
                          f"pass --force to write into it anyway")
    registry = read_registry(cfg)
    sources = registry["sources"]
    if source_id is not None:
        if source_id not in sources:
            raise SourceError(f"unknown source_id: {source_id}")
        selected = {source_id: sources[source_id]}
    else:
        selected = {sid: rec for sid, rec in sources.items() if isinstance(rec, dict)}

    dest.mkdir(parents=True, exist_ok=True)
    copied, missing = [], []
    for sid, rec in sorted(selected.items()):
        rel = rec.get("stored_path")
        if not rec.get("stored") or not rel:
            continue
        src = stored_abspath(cfg, rel)
        if not src.is_file():
            missing.append(sid)
            continue
        out = dest / str(rel).replace("\\", "/").replace(".mb/", "", 1)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        copied.append(rel)

    manifest = {"schema_version": REGISTRY_SCHEMA_VERSION, "sources": selected}
    atomic_write_json(dest / "sources.json", manifest)
    return {"dest": str(dest), "exported": len(copied), "files": copied,
            "missing": missing, "registry": "sources.json"}
