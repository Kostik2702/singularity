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
from typing import Any

import numpy as np

from ..config import Config
from . import lexical
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
        # Built on first hybrid query and reused; a session that never asks for
        # one pays nothing, and daemon start time is unchanged (AUD-17).
        self._bm25: dict[str, Any] = {}
        # W03: ingested raw sources — a third corpus, opted into per query
        self.src_embs: np.ndarray | None = None
        self.src_chunks: list[dict[str, Any]] = []
        self.sources_warning: str | None = None
        self.code_warning: str | None = None
        self.reload()

    # ------------------------------------------------------------------
    def _load_one(self, index_dir: Path
                  ) -> tuple[np.ndarray | None, list[dict], str | None]:
        """(normalized embeddings, chunks, model name). The model name is read
        here rather than in a second pass because metadata.json holds every
        chunk and is megabytes on a real vault — reading it twice per reload
        would be pure waste on daemon start."""
        from . import indexer
        files = indexer.index_files(index_dir)   # committed generation or legacy flat
        if files is None:
            return None, [], None
        emb_path, meta_path = files
        try:
            embs = np.load(emb_path, allow_pickle=False).astype(np.float32)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            chunks = meta.get("chunks", [])
            if embs.shape[0] != len(chunks):
                return None, [], None
            # normalize defensively (indexes built elsewhere may be unnormalized)
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            return embs / norms, chunks, meta.get("model")
        except (OSError, ValueError, json.JSONDecodeError):
            return None, [], None

    def _model_mismatch(self, corpus: str, mem_model: str | None,
                        other_model: str | None, fix: str) -> str | None:
        """Cosine between two different embedding spaces is meaningless. Serving
        quiet nonsense is strictly worse than refusing: the caller cannot tell a
        bad match from a mis-built index. Shared by sources (W05) and code (C04)
        so a third corpus cannot forget to check."""
        if mem_model and other_model and other_model != mem_model:
            return (f"{corpus} index was built with model {other_model!r} but the "
                    f"vault index with {mem_model!r}; {corpus} search is disabled "
                    f"until they match — run `{fix}`")
        return None

    def reload(self) -> dict[str, int]:
        self.mem_embs, self.mem_chunks, mem_model = self._load_one(self.cfg.memory_index_dir)
        self.code_embs, self.code_chunks, self.code_warning = None, [], None
        self.src_embs, self.src_chunks, self.sources_warning = None, [], None

        code_embs, code_chunks, code_model = self._load_one(self.cfg.code_index_dir)
        if code_embs is not None:
            self.code_warning = self._model_mismatch(
                "code", mem_model, code_model, "mb index rebuild --code")
            if self.code_warning is None:
                self.code_embs, self.code_chunks = code_embs, code_chunks

        if self.cfg.get("ingest_index_sources"):
            src_embs, src_chunks, src_model = self._load_one(self.cfg.sources_index_dir)
            if src_embs is not None:
                self.sources_warning = self._model_mismatch(
                    "sources", mem_model, src_model, "mb ingest index --rebuild")
            if self.sources_warning is None:
                self.src_embs, self.src_chunks = src_embs, src_chunks

        return {"memory": len(self.mem_chunks), "code": len(self.code_chunks),
                "sources": len(self.src_chunks)}

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
            wanted_links = {ln.strip().lower() for ln in
                            (links if isinstance(links, list) else str(links).split(","))}
            have_links = {str(ln).lower() for ln in meta.get("wikilinks", [])}
            # a note also "links to itself": match by title too
            have_links.add(str(meta.get("title", "")).lower())
            if not wanted_links & have_links:
                return False
        repos = req.get("repos")
        if repos and meta.get("repo") is not None:
            # `repo` is a code-corpus field. A note simply has no repository, so
            # it is not "in the wrong one" — it is outside the concept. Applying
            # the filter to it anyway would make `--repos api` silently return
            # zero notes, i.e. exactly the empty-result-that-looks-like-no-match
            # this epic refuses everywhere else.
            wanted_repos = {r.strip() for r in
                            (repos if isinstance(repos, list) else str(repos).split(","))
                            if str(r).strip()}
            if meta["repo"] not in wanted_repos:
                return False
        proj = req.get("filter_project")
        if proj:
            # exact match like repos/type (registry ids, not free-text tags);
            # a chunk with no project key classifies as "general" (T04 §8.2).
            wanted_projects = set(proj if isinstance(proj, list) else str(proj).split(","))
            if (meta.get("project") or "general") not in wanted_projects:
                return False
        return True

    def _search_source(self, q_emb: np.ndarray, embs: np.ndarray | None,
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
        lexical_hits: set[int] = set()
        fused_score: dict[int, float] | None = None
        if self._hybrid_wanted(req):
            order, lexical_hits, fused_score = self._fuse_with_lexical(
                order, chunks, source, req, pool)
        results = []
        for idx in order:
            score = float(scores[idx])
            if score < self.cfg.min_score:
                # Pure vector search walks a monotonically decreasing list, so
                # the first miss ends it. Fusion reorders, so `break` there
                # would throw away exactly what the lexical half found: an
                # exact-token hit whose cosine is legitimately low. Such a hit
                # survives; anything neither half supports is skipped.
                if idx not in lexical_hits and not lexical_hits:
                    break
                if idx not in lexical_hits:
                    continue
            meta = chunks[idx]
            if not self._match_filters(meta, req):
                continue
            item = {"meta": meta, "score": score, "source": source}
            if fused_score is not None:
                item["fused"] = fused_score.get(int(idx), 0.0)
            results.append(item)
            if len(results) >= top_k:
                break
        return results

    # --- hybrid retrieval (AUD-17) -------------------------------------

    def _hybrid_wanted(self, req: dict[str, Any]) -> bool:
        """Per-request `hybrid` wins over the config default, so a caller can
        ask for exact-token recall without changing the machine's setting."""
        explicit = req.get("hybrid")
        if explicit is not None:
            return bool(explicit)
        return bool(self.cfg.get("hybrid_search"))

    def _lexical_for(self, source: str, chunks: list[dict[str, Any]]):
        index = self._bm25.get(source)
        if index is None or index[0] != len(chunks):
            index = (len(chunks), lexical.BM25(lexical.document_text(c) for c in chunks))
            self._bm25[source] = index
        return index[1]

    def _fuse_with_lexical(self, order, chunks: list[dict[str, Any]],
                           source: str, req: dict[str, Any], pool: int):
        """Blend the vector ranking with BM25 by RANK, not by score.

        Cosine and BM25 are on incomparable scales, so a weighted sum needs a
        normalisation constant that must be re-tuned per corpus; RRF reads
        positions only. The vector candidates stay in the pool — fusion can
        reorder them and pull in exact-token hits the embedding missed, never
        silently drop what the semantic search found.
        """
        query = str(req.get("query") or req.get("q") or "")
        if not query:
            return order, set(), None
        bm = self._lexical_for(source, chunks)
        lex_rank = lexical.rank_by_score(bm.scores(query), limit=pool)
        if not lex_rank:
            return order, set(), None
        vec_rank = [int(i) for i in order]
        fused = lexical.rrf([vec_rank, lex_rank])
        ranked = [i for i, _ in sorted(fused.items(), key=lambda kv: kv[1], reverse=True)]
        return ranked[:pool], set(lex_rank), fused

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
        # corpus label: source chunks carry type="source" (pinned by
        # chunking.extract_source_chunks), so a raw hit is never mistaken for
        # a wiki note in the compact line.
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

        # Both extra corpora are OPT-IN per query (W03 D2 for sources, C04 D2
        # for code): the wiki is the answer, code and raw documents are the
        # escalation. Opt-in also makes the default result set provably
        # unchanged by this epic, which is what the C00 baseline is compared
        # against.
        want_code = bool(req.get("code_only") or req.get("include_code"))
        results: list[dict[str, Any]] = []
        if not req.get("code_only"):
            results += self._search_source(q_emb, self.mem_embs, self.mem_chunks,
                                           "memory", req)
        if want_code and not req.get("memory_only"):
            results += self._search_source(q_emb, self.code_embs, self.code_chunks,
                                           "code", req)
        if req.get("include_sources"):
            results += self._search_source(q_emb, self.src_embs, self.src_chunks,
                                           "sources", req)
        # Fused results are ordered by RANK, not by cosine: sorting them by
        # score again would undo the fusion and drop exactly the exact-token
        # hits it was added to surface.
        if any("fused" in r for r in results):
            results.sort(key=lambda r: (r.get("fused", 0.0), r["score"]), reverse=True)
        else:
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
                if "fused" in r:
                    out["explain"]["fused"] = round(r["fused"], 5)
            return out

        trimmed = [_one(r) for r in results]
        out = ({"results": trimmed} if req.get("format", "compact") == "json"
               else {"lines": [self._compact_line(r) for r in trimmed]})
        if req.get("include_sources"):
            if self.sources_warning:
                out["sources_warning"] = self.sources_warning
            elif not self.src_chunks:
                out["sources_warning"] = (
                    "no sources indexed — run `mb ingest add <file>` then "
                    "`mb ingest index`")
        # Only ever raised for a query that ASKED for code: a plain `mb search`
        # must stay silent about a corpus the user has not opted into.
        if want_code:
            if self.code_warning:
                out["code_warning"] = self.code_warning
            elif not self.code_chunks:
                out["code_warning"] = (
                    "the code index is empty or its configured root is "
                    "unreachable — check `code_repos_dir`, then run "
                    "`mb index rebuild --code`")
        return out
