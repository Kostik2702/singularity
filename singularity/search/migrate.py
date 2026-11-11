"""`mb model migrate`: move this machine to another embedding model, whole.

Changing `model` in the config is one line, and by itself it breaks search:
the weights are not installed, and every index on the machine was embedded in
the old model's space (the engine refuses to compare across two spaces, so the
code and sources corpora switch themselves off). The steps that make the
change real -- install, re-embed each corpus, restart the daemon -- used to
live in one person's head. Config, weights and indexes are all per machine
(`~/.singularity.json`, `.mb/` is not synced), so each machine has to run them,
and this is the one command that does.

Safe to run twice. A corpus whose index already carries the target model is
skipped, so a run that died half-way resumes instead of starting over. The old
model's weights stay in the cache, and until the notes index is rebuilt a
failure puts the old model back in the config: the machine is never left with
a config that names a model it has no index for.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..config import Config
from . import client, indexer, model_store

#: (corpus argument for `run_index`, label, index directory attribute)
CORPORA = ((None, "notes", "memory_index_dir"),
           ("sources", "sources", "sources_index_dir"),
           ("code", "code", "code_index_dir"))


def plan(cfg: Config, target: str) -> dict[str, Any]:
    """What a migration to `target` would do on this machine. Reads only."""
    corpora = []
    for corpus, label, attr in CORPORA:
        summary = indexer.index_summary(getattr(cfg, attr))
        if summary is None:
            if corpus is not None:
                continue                # a corpus this machine never built
            summary = {}
        corpora.append({"corpus": corpus, "label": label,
                        "model": summary.get("model"),
                        "chunks": summary.get("chunk_count") or 0,
                        "rebuild": summary.get("model") != target})
    return {"from": cfg.model, "to": target, "corpora": corpora,
            "config_change": cfg.model != target,
            "nothing_to_do": cfg.model == target
                             and not any(c["rebuild"] for c in corpora)}


def migrate(cfg: Config, target: str, revision: str | None = None,
            say: Callable[[str], None] = print) -> dict[str, Any]:
    """Run the migration; returns {"ok", "rebuilt", "failed", ...}.

    Raises nothing for an expected failure -- the result says what stopped and
    what state the machine was left in."""
    todo = plan(cfg, target)
    if todo["nothing_to_do"]:
        return {**todo, "ok": True, "rebuilt": [], "failed": None}

    previous = cfg.model
    cfg.data["model"] = target
    try:
        say(f"[1/3] model: installing {target}")
        model_store.install(cfg, revision=revision)
    except Exception as exc:  # noqa: BLE001 - reported, with the config untouched
        cfg.data["model"] = previous
        return {**todo, "ok": False, "rebuilt": [],
                "failed": f"install: {exc}", "config_model": previous}
    cfg.save()

    # The daemon binds its model at start; the next index call starts a fresh
    # one, which reads the config just saved.
    client.stop_daemon(cfg)
    rebuilt: list[str] = []
    for n, item in enumerate(c for c in todo["corpora"] if c["rebuild"]):
        say(f"[2/3] index: re-embedding {item['label']} ({item['chunks']} chunks)")
        try:
            client.run_index(cfg, "rebuild", corpus=item["corpus"])
        except Exception as exc:  # noqa: BLE001
            if n == 0 and item["corpus"] is None:
                # nothing is on the new model yet: put the old one back
                cfg.data["model"] = previous
                cfg.save()
                client.stop_daemon(cfg)
                client.start_daemon(cfg)
                return {**todo, "ok": False, "rebuilt": rebuilt,
                        "failed": f"{item['label']}: {exc}", "config_model": previous}
            return {**todo, "ok": False, "rebuilt": rebuilt,
                    "failed": f"{item['label']}: {exc}", "config_model": target,
                    "resume": "run `mb model migrate` again: finished corpora are skipped"}
        rebuilt.append(item["label"])

    if target == model_store.RECOMMENDED_MODEL and cfg.get("jev_reorder", True) is True:
        # the pairing that was measured: with this embedder Jev's order costs
        # recall@1 (0.711 -> 0.658) while its filter still helps (`jev_reorder`)
        cfg.data["jev_reorder"] = False
        cfg.save()
    say("[3/3] daemon: restarting")
    client.stop_daemon(cfg)
    daemon_ok = client.start_daemon(cfg)
    return {**todo, "ok": daemon_ok, "rebuilt": rebuilt, "config_model": target,
            "failed": None if daemon_ok else "daemon did not start"}
