"""Memory curator (MB-10): vault health + semantic link suggestions.

Two layers (port of Genevra memory-validation + memory-doctor):
  1. Mechanics (zero tokens, no model): broken wikilinks, mandatory
     frontmatter, decisions without owner/source, duplicate titles.
  2. Semantics (reuses the EXISTING index, no re-encoding): note-level
     embeddings = mean of chunk embeddings -> cosine pairs above threshold
     that have no wikilink in either direction -> link SUGGESTIONS.
     Plus orphans (no in/out links) and stale active notes.

The curator NEVER writes to the vault — it only reports/suggests
(Never Decide Alone for structure; a human or the assistant applies
approved suggestions).
"""
from __future__ import annotations

import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from .config import Config
from .search.chunking import parse_frontmatter, _WIKILINK_RE

# type is enforced only where the note contract applies (MB-08 folders);
# project/analysis docs need just `created`; title may come from H1
CONTRACT_FOLDERS = ("learnings", "decisions", "howto", "incidents",
                    "rules", "reference", "daily")
SUGGEST_THRESHOLD = 0.65
SUGGEST_LIMIT = 15
STALE_DAYS = 60
# housekeeping notes: excluded from orphan/suggestion analysis (not from validation)
EXCLUDED_PREFIXES = ("_moc/", "changelog/", "daily/", "templates/")


def _scan_notes(cfg: Config) -> dict[str, dict[str, Any]]:
    """rel_path -> {frontmatter, title, aliases, outlinks, mtime} for all vault notes."""
    skip_dirs = set(cfg.get("skip_dirs")) - {"templates"}  # validate templates too? no:
    skip_dirs.add("templates")
    notes: dict[str, dict[str, Any]] = {}
    for path in cfg.vault.rglob("*.md"):
        rel = path.relative_to(cfg.vault).as_posix()
        first = rel.split("/", 1)[0]
        if first in skip_dirs:
            continue
        try:
            raw = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm, body = parse_frontmatter(raw)
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        title = (fm.get("title") or (m.group(1).strip() if m else path.stem))
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [a.strip() for a in aliases.strip("[]").split(",") if a.strip()]
        # wikilinks only OUTSIDE code: [[..]] inside fenced blocks / `inline code`
        # is syntax examples (bash [[ tests ]]), not links
        linkable = re.sub(r"```.*?```", " ", body, flags=re.DOTALL)
        linkable = re.sub(r"`[^`\n]*`", " ", linkable)
        linkable = linkable.replace("\\|", "|")  # escaped pipe in md tables
        outlinks = [mt.group(1).strip() for mt in _WIKILINK_RE.finditer(linkable)]
        outlinks = [l for l in outlinks if l and l != "..."]
        notes[rel] = {
            "fm": fm, "title": str(title).strip('"'), "aliases": aliases,
            "outlinks": outlinks, "mtime": path.stat().st_mtime,
        }
    return notes


def _name_index(notes: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Resolvable link name (lowercase) -> rel_path. Stems, titles, aliases."""
    idx: dict[str, str] = {}
    for rel, info in notes.items():
        idx[Path(rel).stem.lower()] = rel
        idx[str(info["title"]).lower()] = rel
        for a in info["aliases"]:
            idx[str(a).lower()] = rel
    return idx


# ---------------------------------------------------------------------------
# Layer 1 — mechanics
# ---------------------------------------------------------------------------

def validate(cfg: Config) -> dict[str, list]:
    notes = _scan_notes(cfg)
    names = _name_index(notes)
    issues: dict[str, list] = {"broken_links": [], "missing_frontmatter": [],
                               "decision_no_owner": [], "duplicate_titles": []}

    titles: dict[str, list[str]] = defaultdict(list)
    for rel, info in notes.items():
        fm = info["fm"]
        missing = []
        if not fm.get("created"):
            missing.append("created")
        if not fm.get("title") and info["title"] == Path(rel).stem:
            missing.append("title|H1")
        if rel.split("/", 1)[0] in CONTRACT_FOLDERS and not fm.get("type"):
            missing.append("type")
        if missing:
            issues["missing_frontmatter"].append({"file": rel, "missing": missing})
        if fm.get("type") == "decision" and not (fm.get("owner") and fm.get("source")):
            issues["decision_no_owner"].append({"file": rel})
        titles[str(info["title"]).lower()].append(rel)
        for link in info["outlinks"]:
            if link.lower() not in names:
                issues["broken_links"].append({"file": rel, "link": link})
    for title, files in titles.items():
        if len(files) > 1:
            issues["duplicate_titles"].append({"title": title, "files": files})
    return issues


# ---------------------------------------------------------------------------
# Layer 2 — semantics (from the existing index, no model needed)
# ---------------------------------------------------------------------------

def _note_embeddings(cfg: Config) -> tuple[list[str], np.ndarray]:
    emb_path = cfg.memory_index_dir / "embeddings.npy"
    meta_path = cfg.memory_index_dir / "metadata.json"
    embs = np.load(emb_path, allow_pickle=False)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    by_file: dict[str, list[int]] = defaultdict(list)
    for i, chunk in enumerate(meta["chunks"]):
        by_file[chunk["file"]].append(i)
    files, vectors = [], []
    for rel, idxs in by_file.items():
        if rel.startswith(EXCLUDED_PREFIXES):
            continue
        v = embs[idxs].mean(axis=0)
        norm = np.linalg.norm(v)
        if norm > 0:
            files.append(rel)
            vectors.append(v / norm)
    return files, (np.vstack(vectors) if vectors else np.zeros((0, embs.shape[1])))


def analyze(cfg: Config) -> dict[str, Any]:
    """Orphans, stale notes, and wikilink suggestions."""
    notes = _scan_notes(cfg)
    names = _name_index(notes)

    # link graph (resolved)
    outgoing: dict[str, set[str]] = {rel: set() for rel in notes}
    incoming: dict[str, set[str]] = {rel: set() for rel in notes}
    for rel, info in notes.items():
        for link in info["outlinks"]:
            target = names.get(link.lower())
            if target and target != rel:
                outgoing[rel].add(target)
                incoming[target].add(rel)

    orphans = [rel for rel in notes
               if not outgoing[rel] and not incoming[rel]
               and not rel.startswith(EXCLUDED_PREFIXES)]

    now = time.time()
    stale = [rel for rel, info in notes.items()
             if info["fm"].get("status") == "active"
             and (now - info["mtime"]) > STALE_DAYS * 86400]

    suggestions: list[dict[str, Any]] = []
    try:
        files, vecs = _note_embeddings(cfg)
    except (OSError, ValueError, json.JSONDecodeError):
        files, vecs = [], np.zeros((0, 1))
    if len(files) >= 2:
        sim = vecs @ vecs.T
        np.fill_diagonal(sim, 0.0)
        pairs = np.argwhere(sim > SUGGEST_THRESHOLD)
        scored = sorted(((float(sim[i, j]), i, j) for i, j in pairs if i < j),
                        reverse=True)
        for score, i, j in scored:
            a, b = files[i], files[j]
            if a in notes and b in notes:
                if b in outgoing.get(a, set()) or a in outgoing.get(b, set()):
                    continue
            suggestions.append({"a": a, "b": b, "score": round(score, 3),
                                "suggest": f"[[{Path(b).stem}]] <-> [[{Path(a).stem}]]"})
            if len(suggestions) >= SUGGEST_LIMIT:
                break

    return {"notes": len(notes), "orphans": sorted(orphans), "stale": sorted(stale),
            "link_suggestions": suggestions}


# ---------------------------------------------------------------------------

def run(cfg: Config, as_json: bool = False) -> int:
    issues = validate(cfg)
    report = analyze(cfg)
    hard = sum(len(v) for v in issues.values())

    if as_json:
        print(json.dumps({"issues": issues, **report}, ensure_ascii=False, indent=1))
        return 1 if issues["broken_links"] else 0

    print(f"== mb curator — {report['notes']} notes ==")
    for key, label in (("broken_links", "Broken wikilinks"),
                       ("missing_frontmatter", "Missing mandatory frontmatter"),
                       ("decision_no_owner", "Decisions without owner/source"),
                       ("duplicate_titles", "Duplicate titles")):
        items = issues[key]
        print(f"\n[{'FAIL' if items and key == 'broken_links' else 'WARN' if items else 'PASS'}] "
              f"{label}: {len(items)}")
        for it in items[:10]:
            print(f"   - {it}")

    print(f"\nOrphans (no links in/out): {len(report['orphans'])}")
    for rel in report["orphans"][:10]:
        print(f"   - {rel}")
    print(f"Stale active notes (>{STALE_DAYS}d): {len(report['stale'])}")
    for rel in report["stale"][:10]:
        print(f"   - {rel}")

    print(f"\nLink suggestions (cosine>{SUGGEST_THRESHOLD}, no existing link) — "
          f"NOT applied, approve first:")
    if not report["link_suggestions"]:
        print("   (none)")
    for s in report["link_suggestions"]:
        print(f"   {s['score']:.2f}  {s['a']}  <->  {s['b']}")
    return 1 if issues["broken_links"] else 0
