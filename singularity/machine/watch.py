"""On-save vault watcher: incremental index update + daemon reload.

Debounced (quiet period) so a burst of Obsidian saves triggers one update.
Cross-platform via watchdog.
"""
from __future__ import annotations

import time
from pathlib import Path

from ..config import Config
from ..log import log

DEBOUNCE_SECONDS = 3.0
HEARTBEAT_FILE = "watch.heartbeat"

JOB_NAME = "watch"


def watch_job(cfg: Config):
    """The at-login watcher job — single source shared by the installer and
    `mb doctor` C2, so the registered argv can't drift from intent (the
    AUD-01 breakage class: a task stored with an unquoted vault path)."""
    from ..platform.scheduler import Job

    return Job(JOB_NAME,
               ["-m", "singularity", "--vault", str(cfg.vault), "watch"],
               at_login=True)


def _write_heartbeat(cfg: Config) -> None:
    """Touch a heartbeat file so `mb doctor` can tell the watcher is alive
    (freshness of this file = liveness). Never let a heartbeat failure kill
    the watcher loop."""
    try:
        hb = cfg.watch_heartbeat_file
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


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
    _write_heartbeat(cfg)
    log.info(f"[watch] watching {cfg.vault} (debounce {DEBOUNCE_SECONDS}s)")
    try:
        while True:
            time.sleep(1.0)
            _write_heartbeat(cfg)
            if not pending:
                continue
            newest = max(pending.values())
            if time.monotonic() - newest < DEBOUNCE_SECONDS:
                continue
            _process_pending(cfg, pending)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()


def _process_pending(cfg: Config, pending: dict[str, float]) -> None:
    """One debounced batch. On failure -- including a held writer lock (e.g.
    `mb nightly` running) -- the files go BACK into `pending` with a fresh
    timestamp so they retry after the next debounce window instead of
    vanishing silently (T12: "do not lose pending watcher files"). Only the
    files actually handled this batch are cleared on success, so an edit
    that lands on another file while this batch is in flight survives."""
    files = sorted(pending)
    from ..search import client

    try:
        stats = client.run_index(cfg, "update", files)
        log.info(f"[watch] updated {files} -> {stats}")
        for rel in files:
            pending.pop(rel, None)
    except client.RemoteIndexError as exc:
        if exc.error_type != "IndexLockedError":
            log.info(f"[watch] update failed: {exc}")
        else:
            log.info(f"[watch] index locked, will retry: {files}")
        for rel in files:
            pending[rel] = time.monotonic()
    except Exception as exc:  # noqa: BLE001 — watcher must survive bad files
        log.info(f"[watch] update failed: {exc}")
        for rel in files:
            pending[rel] = time.monotonic()
