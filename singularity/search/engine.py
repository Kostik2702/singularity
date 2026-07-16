"""Search engine core: index loading, cosine search, filters, trim_meta,
compact formatting. Used warm by the daemon and cold by the client fallback.

Ported from Genevra search-daemon.py::_search_source/_trim_meta with the
wikilinks filter extension. Embeddings are L2-normalized at encode time,
so cosine similarity == dot product (numpy only, no sklearn).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import numpy as np

from ..config import Config
from .encoder import shared_encoder

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


class SearchEngine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.encoder = shared_encoder(cfg.model, cfg.get("batch_size"), cfg=cfg)
        self.mem_embs: np.ndarray | None = None
        self.mem_chunks: list[dict[str, Any]] = []
        self.code_embs: np.ndarray | None = None
        self.code_chunks: list[dict[str, Any]] = []
        self.reload()

    # ------------------------------------------------------------------
    def _load_one(self, index_dir: Path) -> tuple[Optional[np.ndarray], list[dict]]:
        from . import indexer
        files = indexer.index_files(index_dir)   # committed generation or legacy flat
        if files is None:
            return None, []
        emb_path, meta_path = files
        try:
            embs = np.load(emb_path, allow_pickle=False).astype(np.float32)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            chunks = meta.get("chunks", [])
            if embs.shape[0] != len(chunks):
                return None, []
            # normalize defensively (indexes built elsewhere may be unnormalized)
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return embs / norms, chunks
        except (OSError, ValueError, json.JSONDecodeError):
            return None, []

    def reload(self) -> dict[str, int]:
        self.mem_embs, self.mem_chunks = self._load_one(self.cfg.memory_index_dir)
        self.code_embs, self.code_chunks = self._load_one(self.cfg.code_index_dir)
        return {"memory": len(self.mem_chunks), "code": len(self.code_chunks)}

    # ------------------------------------------------------------------
    @staticmethod
    def _match_filters(meta: dict[str, Any], req: dict[str, Any]) -> bool:
        ft = req.get("filter_type")
        if ft and meta.get("type") != ft:
            return False
        tags = req.get("filter_tags")
        if tags:
            wanted = {t.strip().lower() for t in
                      (tags if isinstance(tags, list) else str(tags).split(","))}
            have = {t.strip().lower() for t in str(meta.get("tags", "")).split(",") if t}
            if not wanted & have:
                return False
        prefix = req.get("path_prefix")
        if prefix and not str(meta.get("file", "")).startswith(prefix):
            return False
        links = req.get("filter_wikilinks")
        if links:
            wanted_links = {l.strip().lower() for l in
                            (links if isinstance(links, list) else str(links).split(","))}
            have_links = {str(l).lower() for l in meta.get("wikilinks", [])}
            # a note also "links to itself": match by title too
            have_links.add(str(meta.get("title", "")).lower())
            if not wanted_links & have_links:
                return False
        repos = req.get("repos")
        if repos:
            wanted_repos = set(repos if isinstance(repos, list) else str(repos).split(","))
            if meta.get("repo") not in wanted_repos:
                return False
        proj = req.get("filter_project")
        if proj:
            # exact match like repos/type (registry ids, not free-text tags);
            # a chunk with no project key classifies as "general" (T04 §8.2).
            wanted_projects = set(proj if isinstance(proj, list) else str(proj).split(","))
            if (meta.get("project") or "general") not in wanted_projects:
                return False
        return True

    def _search_source(self, q_emb: np.ndarray, embs: Optional[np.ndarray],
                       chunks: list[dict], source: str,
                       req: dict[str, Any]) -> list[dict[str, Any]]:
        if embs is None or embs.shape[0] == 0:
            return []
        top_k = int(req.get("top_k", 5))
        has_filters = any(req.get(k) for k in
                          ("filter_type", "filter_tags", "path_prefix",
                           "filter_wikilinks", "repos", "filter_project"))
        pool = top_k * (20 if has_filters else 3)   # Genevra candidate pool
        scores = embs @ q_emb                        # cosine (normalized)
        order = np.argsort(scores)[::-1][:pool]
        results = []
        for idx in order:
            score = float(scores[idx])
            if score < self.cfg.min_score:
                break
            meta = chunks[idx]
            if not self._match_filters(meta, req):
                continue
            results.append({"meta": meta, "score": score, "source": source})
            if len(results) >= top_k:
                break
        return results

    # ------------------------------------------------------------------
    def _trim_meta(self, meta: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
        """Token-economy trim (Genevra): drop denormalized/empty/default fields,
        strip frontmatter from content, cut to excerpt."""
        out = {}
        excerpt = int(req.get("excerpt_chars", self.cfg.get("excerpt_chars")))
        full = bool(req.get("full_content", False))
        for key, val in meta.items():
            if key == "text":
                continue
            if val in (None, "", [], {}):
                continue
            if key == "type" and val == "reference":
                continue
            if key == "content":
                val = _FRONTMATTER_RE.sub("", str(val))
                if not full and len(val) > excerpt:
                    val = val[:excerpt] + "..."
            out[key] = val
        if out.get("title") and out.get("title") == out.get("section"):
            out.pop("section")   # dedupe title==section (~10% savings)
        return out

    @staticmethod
    def _compact_line(item: dict[str, Any]) -> str:
        meta = item["meta"]
        loc = meta.get("file", "?")
        if meta.get("line"):
            loc = f"{loc}:{meta['line']}"
        tag = meta.get("type") or meta.get("kind") or item["source"]
        if meta.get("project"):
            tag = f"{tag}@{meta['project']}"
        head = meta.get("title", "")
        if meta.get("section"):
            head = f"{head}/{meta['section']}"
        elif meta.get("name"):
            head = meta["name"]
        return f"{loc} | {item['score']:.2f} | [{tag}] | {head} | {meta.get('content', '')}"

    # ------------------------------------------------------------------
    def search(self, req: dict[str, Any]) -> dict[str, Any]:
        query = str(req.get("query", "")).strip()
        if not query:
            return {"error": "empty query"}
        top_k = int(req.get("top_k", 5))
        q_emb = self.encoder.encode([query])[0]

        results: list[dict[str, Any]] = []
        sources = 0
        if not req.get("code_only"):
            results += self._search_source(q_emb, self.mem_embs, self.mem_chunks,
                                           "memory", req)
            sources += 1
        if not req.get("memory_only"):
            results += self._search_source(q_emb, self.code_embs, self.code_chunks,
                                           "code", req)
            sources += 1
        results.sort(key=lambda r: r["score"], reverse=True)

        # Per-file diversity: cap chunks per file so one long note's many windows
        # (AUD-03) can't flood the results; order within is unchanged.
        max_per_file = int(req.get("max_per_file", 2))
        if max_per_file > 0:
            seen: dict[str, int] = {}
            diverse = []
            for r in results:
                f = str(r["meta"].get("file", ""))
                if seen.get(f, 0) >= max_per_file:
                    continue
                seen[f] = seen.get(f, 0) + 1
                diverse.append(r)
            results = diverse

        results = results[:top_k]          # strict global top-k

        explain = bool(req.get("explain"))

        def _one(r):
            out = {"meta": self._trim_meta(r["meta"], req),
                   "score": round(r["score"], 4),
                   "source": r["source"]}
            if explain:
                out["explain"] = {"semantic": round(r["score"], 4)}
            return out

        trimmed = [_one(r) for r in results]
        if req.get("format", "compact") == "json":
            return {"results": trimmed}
        return {"lines": [self._compact_line(r) for r in trimmed]}
