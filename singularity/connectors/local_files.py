"""Reference connector: a local directory of .md/.txt files (AUD-22).

Proves the contract end-to-end with no network/secrets. Incremental cursor is
the max mtime seen, so a later fetch returns only files modified since.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import Connector, ConnectorManifest, NormalizedDoc

_EXTS = {".md", ".txt"}


class LocalFilesConnector(Connector):
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def manifest(self) -> ConnectorManifest:
        return ConnectorManifest(
            name="local_files",
            capabilities=["fetch", "incremental"],
            config_schema={"root": "path"},
            secret_refs=[],                       # local files need no secret
            rate_limit_per_min=None,
            permissions=["read"],
        )

    def health(self) -> dict[str, Any]:
        ok = self.root.is_dir()
        return {"ok": ok, "detail": str(self.root) if ok else f"not a directory: {self.root}"}

    def fetch(self, cursor: str | None = None
              ) -> tuple[list[NormalizedDoc], str | None]:
        since = float(cursor) if cursor else -1.0
        docs: list[NormalizedDoc] = []
        newest = since
        if not self.root.is_dir():
            return docs, cursor
        for p in sorted(self.root.rglob("*")):
            if p.suffix.lower() not in _EXTS or not p.is_file():
                continue
            mtime = p.stat().st_mtime
            if mtime <= since:
                continue                          # incremental: unchanged, skip
            newest = max(newest, mtime)
            rel = p.relative_to(self.root).as_posix()
            docs.append(NormalizedDoc(
                id=rel, title=p.stem,
                text=p.read_text(encoding="utf-8", errors="ignore"),
                source="local_files", metadata={"path": rel, "mtime": mtime}))
        next_cursor = str(newest) if newest > since else cursor
        return docs, next_cursor
