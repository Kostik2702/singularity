"""On-save vault watcher: incremental index update + daemon reload.

Debounced (quiet period) so a burst of Obsidian saves triggers one update.
Cross-platform via watchdog.
"""
from __future__ import annotations

import time
from pathlib import Path

from .config import Config

DEBOUNCE_SECONDS = 3.0


def run(cfg: Config) -> None:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer

    pending: dict[str, float] = {}
    skip_dirs = set(cfg.get("skip_dirs"))

    class Handler(FileSystemEventHandler):
        def _mark(self, src: str) -> None:
            p = Path(src)
            if p.suffix.lower() != ".md":
                return
            try:
                rel = p.relative_to(cfg.vault).as_posix()
            except ValueError:
                return
            if rel.split("/", 1)[0] in skip_dirs:
                return
            pending[rel] = time.monotonic()

        def on_modified(self, event):
            if not event.is_directory:
                self._mark(event.src_path)

        def on_created(self, event):
            if not event.is_directory:
                self._mark(event.src_path)

        def on_deleted(self, event):
            if not event.is_directory:
                self._mark(event.src_path)

        def on_moved(self, event):
            if not event.is_directory:
                self._mark(event.src_path)
                self._mark(event.dest_path)

    observer = Observer()
    observer.schedule(Handler(), str(cfg.vault), recursive=True)
    observer.start()
    print(f"[watch] watching {cfg.vault} (debounce {DEBOUNCE_SECONDS}s)", flush=True)
    try:
        while True:
            time.sleep(1.0)
            if not pending:
                continue
            newest = max(pending.values())
            if time.monotonic() - newest < DEBOUNCE_SECONDS:
                continue
            files = sorted(pending)
            pending.clear()
            from .search import indexer
            from .search import client

            try:
                stats = indexer.update(cfg, only_files=files)
                client.reload_daemon(cfg)
                print(f"[watch] updated {files} -> {stats}", flush=True)
            except Exception as exc:  # noqa: BLE001 — watcher must survive bad files
                print(f"[watch] update failed: {exc}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
