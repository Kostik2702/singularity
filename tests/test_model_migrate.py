"""`mb model migrate`: one command moves a machine to another embedder, and a
failure never leaves the config naming a model the machine has no index for."""
from types import SimpleNamespace

import pytest

from singularity import cli
from singularity.config import Config
from singularity.search import client, indexer, migrate, model_store

OLD = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
NEW = model_store.RECOMMENDED_MODEL


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A machine with notes and code indexed by OLD and no sources corpus."""
    cfg = Config(vault=tmp_path, data={"model": OLD})
    state = {"index": {"memory_index_dir": OLD, "code_index_dir": OLD}, "calls": [],
             "fail_install": False, "fail_corpus": None, "saved": []}

    def summary(index_dir):
        for attr, model in state["index"].items():
            if index_dir == getattr(cfg, attr):
                return {"model": model, "chunk_count": 10}
        return None

    def install(c, revision=None, force=False):
        state["calls"].append(("install", c.model, revision))
        if state["fail_install"]:
            raise RuntimeError("no network")
        return {"installed": True}

    def run_index(c, action, files=None, corpus=None):
        state["calls"].append(("index", corpus, c.model))
        if (corpus or "notes") == state["fail_corpus"]:
            raise client.RemoteIndexError("boom")
        state["index"]["code_index_dir" if corpus == "code" else "memory_index_dir"] = c.model
        return {}

    monkeypatch.setattr(indexer, "index_summary", summary)
    monkeypatch.setattr(model_store, "install", install)
    monkeypatch.setattr(client, "run_index", run_index)
    monkeypatch.setattr(client, "stop_daemon", lambda c: state["calls"].append(("stop",)) or True)
    monkeypatch.setattr(client, "start_daemon", lambda c: state["calls"].append(("start",)) or True)
    monkeypatch.setattr(Config, "save", lambda self: state["saved"].append(dict(self.data)))
    return cfg, state


def quiet(_line):
    pass


def test_the_plan_lists_only_corpora_this_machine_built(machine):
    cfg, _ = machine
    todo = migrate.plan(cfg, NEW)
    assert [c["label"] for c in todo["corpora"]] == ["notes", "code"]
    assert all(c["rebuild"] for c in todo["corpora"]) and not todo["nothing_to_do"]


def test_a_migration_installs_reembeds_everything_and_restarts(machine):
    cfg, state = machine
    out = migrate.migrate(cfg, NEW, "rev1", say=quiet)
    assert out["ok"] and out["rebuilt"] == ["notes", "code"]
    assert state["calls"][0] == ("install", NEW, "rev1")
    assert [c for c in state["calls"] if c[0] == "index"] == [("index", None, NEW),
                                                             ("index", "code", NEW)]
    assert state["calls"][-2:] == [("stop",), ("start",)]
    assert cfg.model == NEW and state["saved"][-1]["model"] == NEW
    assert cfg.get("jev_reorder") is False      # the measured pairing for this embedder


def test_a_failed_install_changes_nothing(machine):
    cfg, state = machine
    state["fail_install"] = True
    out = migrate.migrate(cfg, NEW, say=quiet)
    assert not out["ok"] and "install" in out["failed"]
    assert cfg.model == OLD and state["saved"] == []
    assert not [c for c in state["calls"] if c[0] == "index"]


def test_a_failed_notes_rebuild_puts_the_old_model_back(machine):
    cfg, state = machine
    state["fail_corpus"] = "notes"
    out = migrate.migrate(cfg, NEW, say=quiet)
    assert not out["ok"] and out["config_model"] == OLD
    assert cfg.model == OLD and state["saved"][-1]["model"] == OLD
    assert state["calls"][-1] == ("start",)     # search is back up on the old model


def test_a_later_failure_keeps_the_new_model_and_a_rerun_resumes(machine):
    cfg, state = machine
    state["fail_corpus"] = "code"
    out = migrate.migrate(cfg, NEW, say=quiet)
    assert not out["ok"] and out["rebuilt"] == ["notes"] and out["config_model"] == NEW
    assert "resume" in out

    state["fail_corpus"] = None
    state["calls"].clear()
    again = migrate.migrate(cfg, NEW, say=quiet)
    assert again["ok"] and again["rebuilt"] == ["code"]
    assert [c for c in state["calls"] if c[0] == "index"] == [("index", "code", NEW)]


def test_a_second_run_does_nothing(machine):
    cfg, state = machine
    migrate.migrate(cfg, NEW, say=quiet)
    state["calls"].clear()
    out = migrate.migrate(cfg, NEW, say=quiet)
    assert out["ok"] and out["nothing_to_do"] and state["calls"] == []


def test_dry_run_changes_nothing(machine, capsys):
    cfg, state = machine
    rc = cli.cmd_model(cfg, SimpleNamespace(model_action="migrate", to=None, revision=None,
                                            dry_run=True, json=False, force=False))
    out = capsys.readouterr().out
    assert rc == 0 and "re-embed" in out and "nothing was changed" in out
    assert state["calls"] == [] and cfg.model == OLD


def test_the_cli_pins_the_recommended_revision(machine):
    cfg, state = machine
    rc = cli.cmd_model(cfg, SimpleNamespace(model_action="migrate", to=None, revision=None,
                                            dry_run=False, json=True, force=False))
    assert rc == 0
    assert state["calls"][0] == ("install", NEW, model_store.RECOMMENDED_REVISION)


def test_the_download_skips_exports_this_engine_never_loads(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={"model": NEW})
    seen = []

    def fake(**kw):
        seen.append(kw)
        if kw["local_files_only"]:
            raise OSError("not cached")
        raise RuntimeError("stop here")

    monkeypatch.setattr(model_store, "_snapshot_download", fake)
    with pytest.raises(RuntimeError):
        model_store.install(cfg)
    assert "onnx/*" in seen[-1]["ignore_patterns"]
