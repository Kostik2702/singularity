"""C09: the index must never be destroyed by an incomplete scan, and vendored
third-party trees must not be indexed.

Both properties come from the adversarial design review of the multi-root
proposal. The multi-root change itself was dropped, but two of its blockers
turned out to be live bugs in the SHIPPED single-root code:

  * a corpus root that is momentarily unavailable (unmounted drive, synced
    folder mid-reconnect) makes `_scan_corpus` return `{}`, which `_update`
    reads as "every file was deleted" and faithfully purges;
  * `os.walk` without an `onerror` handler swallows permission errors and
    yields nothing for that subtree — the same purge, with no signal at all.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import singularity.search.indexer as indexer_mod
from singularity.config import Config
from singularity.search import code_chunking


class FakeEncoder:
    model_name = "fake"

    def encode(self, texts, progress=False):
        return np.ones((len(texts), 4), dtype=np.float32)


@pytest.fixture
def built(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    repos = tmp_path / "Project"
    (repos / "api").mkdir(parents=True)
    (repos / "api" / "orders.py").write_text(
        "def create_order(payload):\n"
        '    """Create an order and return its id."""\n'
        "    return payload\n", encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault",
                 data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    return cfg


def _chunks(cfg):
    _, meta_path = indexer_mod.index_files(cfg.code_index_dir)
    return json.loads(meta_path.read_text(encoding="utf-8"))["chunks"]


# --- an unavailable root must never look like a deletion ---------------------

def test_update_refuses_when_the_root_disappeared_and_keeps_every_chunk(built):
    before = len(_chunks(built))
    assert before > 0
    current = (built.code_index_dir / "CURRENT").read_text(encoding="utf-8")

    import shutil
    shutil.rmtree(built.code_repos_dir)

    with pytest.raises(indexer_mod.CorpusRootUnavailable) as exc:
        indexer_mod.update(built, corpus="code")
    assert "refusing" in str(exc.value)
    assert len(_chunks(built)) == before
    assert (built.code_index_dir / "CURRENT").read_text(encoding="utf-8") == current


def test_rebuild_refuses_over_a_populated_index_when_the_root_disappeared(built):
    before = len(_chunks(built))
    import shutil
    shutil.rmtree(built.code_repos_dir)

    with pytest.raises(indexer_mod.CorpusRootUnavailable):
        indexer_mod.rebuild(built, progress=False, corpus="code")
    assert len(_chunks(built)) == before


def test_bootstrapping_an_empty_corpus_over_a_missing_root_is_still_allowed(
        tmp_path, monkeypatch):
    """`mb ingest index` on a vault that never ingested anything must keep
    working — there is nothing to lose, so the guard must not fire."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    cfg = Config(vault=tmp_path / "vault", data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    stats = indexer_mod.rebuild(cfg, progress=False, corpus="sources")
    assert stats["files"] == 0 and stats["chunks"] == 0


def test_a_walk_error_is_not_treated_as_a_deletion(built, monkeypatch):
    """A denied ACL yields nothing for that subtree, exactly like a deletion.
    Simulated here because ACL manipulation is not portable."""
    before = len(_chunks(built))
    real_walk = indexer_mod.os.walk

    def exploding_walk(top, onerror=None, **kw):
        if onerror is not None:
            onerror(PermissionError(13, "Access is denied", str(top)))
        return iter(())

    monkeypatch.setattr(indexer_mod.os, "walk", exploding_walk)
    with pytest.raises(indexer_mod.CorpusRootUnavailable) as exc:
        indexer_mod.update(built, corpus="code")
    assert "could not be read" in str(exc.value)
    monkeypatch.setattr(indexer_mod.os, "walk", real_walk)
    assert len(_chunks(built)) == before


def test_scan_records_walk_errors_in_the_report(built, monkeypatch):
    def exploding_walk(top, onerror=None, **kw):
        if onerror is not None:
            onerror(PermissionError(13, "Access is denied", str(top)))
        return iter(())

    monkeypatch.setattr(indexer_mod.os, "walk", exploding_walk)
    report: dict = {}
    corp = indexer_mod.code_corpus(built)
    assert indexer_mod._scan_corpus(corp, report) == {}
    assert len(report["walk_errors"]) == 1
    assert "Access is denied" in report["walk_errors"][0]["error"]


def test_daemon_maps_the_refusal_to_its_own_error_type(built, monkeypatch):
    from singularity.search import daemon

    d = daemon.SearchDaemon(built)
    d.engine = type("E", (), {"reload": lambda self: {}})()

    def boom(*_a, **_k):
        raise indexer_mod.CorpusRootUnavailable("root gone")

    monkeypatch.setattr(indexer_mod, "update", boom)
    out = d._handle({"cmd": "index", "action": "update", "corpus": "code"})
    assert out["error_type"] == "CorpusRootUnavailable"


def test_nightly_turns_the_refusal_into_a_skip_not_an_error(built, monkeypatch):
    from singularity import nightly
    from singularity.search import client

    def boom(*_a, **_k):
        raise client.RemoteIndexError("root gone",
                                      error_type="CorpusRootUnavailable")

    monkeypatch.setattr(client, "run_index", boom)
    out = nightly._step_code_index(built)
    assert out["status"] == "skipped" and "root gone" in out["reason"]


# --- vendored third-party trees ---------------------------------------------

VENDORED = ["renpy-8.5.2-sdk", "node-v18.17.1-win-x64", "python-3.11.4",
            "jdk-17.0.2", "libfoo_2.4.1"]
OWN_CODE = ["core-2.0", "lr-0.001", "sd-1.5", "FlashVSR-v1.1", "api-v2",
            "Krea2 Nodes", "matrix-3d-web", "v2", "chart-3d", "src", "app"]
# Documented misses: the version must be DOT-separated, so the boost-style
# `boost_1_82_0` is not recognized. Widening to `_` separators would also match
# ordinary names like `layer_2_3`, and this rule's false positives are silent
# while its misses are visible — so the miss is the correct trade.
KNOWN_MISSES = ["boost_1_82_0", "node-v20", "Qt5.15"]


@pytest.mark.parametrize("name", VENDORED)
def test_vendored_directory_names_match(name):
    assert indexer_mod._regex_matcher(code_chunking.VENDORED_DIR_PATTERN)(name)


@pytest.mark.parametrize("name", OWN_CODE)
def test_own_code_directory_names_do_not_match(name):
    """The rule errs silently when it over-matches, so a false positive here is
    strictly worse than a miss — two numeric components are not enough."""
    assert not indexer_mod._regex_matcher(code_chunking.VENDORED_DIR_PATTERN)(name)


@pytest.mark.parametrize("name", KNOWN_MISSES)
def test_known_misses_are_pinned_so_the_gap_is_not_rediscovered(name):
    """These ARE vendored trees the rule does not catch. Pinned deliberately:
    the documented remedy is one entry in `code_skip_dirs`, not a looser regex
    that would start eating first-party directories in silence."""
    assert not indexer_mod._regex_matcher(code_chunking.VENDORED_DIR_PATTERN)(name)


def test_a_vendored_subtree_is_never_scanned_and_is_reported_by_name(built):
    sdk = built.code_repos_dir / "novel" / "engine-9.1.3-sdk" / "deep"
    sdk.mkdir(parents=True)
    (sdk / "engine.py").write_text(
        "def engine_entry_point():\n    return 'third party code here'\n",
        encoding="utf-8")
    (built.code_repos_dir / "novel" / "mine.py").write_text(
        "def my_own_function():\n    return 'first party code here'\n",
        encoding="utf-8")

    report: dict = {}
    scanned = indexer_mod._scan_corpus(indexer_mod.code_corpus(built), report)
    assert "novel/mine.py" in scanned
    assert not [k for k in scanned if "engine-9.1.3-sdk" in k]
    assert report["excluded"] == [{"kind": "vendored",
                                   "path": "novel/engine-9.1.3-sdk"}]


def test_the_exclusion_is_recorded_by_both_rebuild_and_update(built):
    """`_update` re-commits the loaded meta verbatim, so a rebuild-only field
    would be carried forward stale into every later nightly."""
    sdk = built.code_repos_dir / "engine-9.1.3-sdk"
    sdk.mkdir()
    (sdk / "x.py").write_text("def third_party():\n    return 'vendored'\n",
                              encoding="utf-8")

    stats = indexer_mod.rebuild(built, progress=False, corpus="code")
    assert stats["excluded"] == ["engine-9.1.3-sdk"]
    _, meta_path = indexer_mod.index_files(built.code_index_dir)
    assert json.loads(meta_path.read_text(encoding="utf-8"))["excluded"] == [
        {"kind": "vendored", "path": "engine-9.1.3-sdk"}]

    import time
    time.sleep(0.01)
    (built.code_repos_dir / "api" / "orders.py").write_text(
        "def create_order(payload):\n    return None\n", encoding="utf-8")
    upd = indexer_mod.update(built, corpus="code")
    assert upd["added"] == 1 and upd["excluded"] == ["engine-9.1.3-sdk"]
    _, meta_path = indexer_mod.index_files(built.code_index_dir)
    assert json.loads(meta_path.read_text(encoding="utf-8"))["excluded"] == [
        {"kind": "vendored", "path": "engine-9.1.3-sdk"}]


def test_code_skip_dirs_prunes_an_extra_name_at_any_depth(built):
    deep = built.code_repos_dir / "api" / "generated" / "nested"
    deep.mkdir(parents=True)
    (deep / "stub.py").write_text("def generated_stub():\n    return 'noise'\n",
                                  encoding="utf-8")
    assert "api/generated/nested/stub.py" in indexer_mod._scan_corpus(
        indexer_mod.code_corpus(built))

    built.data["code_skip_dirs"] = ["generated"]
    scanned = indexer_mod._scan_corpus(indexer_mod.code_corpus(built))
    assert not [k for k in scanned if "generated" in k]
    assert "api/orders.py" in scanned


def test_code_skip_dirs_is_a_known_validated_key():
    from singularity.config import DEFAULT_CONFIG, validate_config

    assert DEFAULT_CONFIG["code_skip_dirs"] == []
    assert validate_config({"code_skip_dirs": ["a", "b"]})["warnings"] == []
    assert validate_config({"code_skip_dirs": "nope"})["errors"]


# --- linked-in repositories (the cross-platform half) ------------------------

def test_a_symlinked_repository_is_indexed(built):
    """Windows junctions are traversed by os.walk regardless (islink=False), but
    a POSIX symlink is not — so the documented "link a repo in from outside the
    root" workaround worked on Windows and silently indexed NOTHING on
    macOS/Linux until the code corpus opted into followlinks."""
    outside = built.vault.parent / "outside-repo"
    (outside / "pkg").mkdir(parents=True)
    (outside / "pkg" / "mod.py").write_text(
        "def linked_in_function():\n    return 'reachable only through the link'\n",
        encoding="utf-8")
    link = built.code_repos_dir / "linked"
    try:
        import os as _os
        _os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")

    scanned = indexer_mod._scan_corpus(indexer_mod.code_corpus(built))
    assert "linked/pkg/mod.py" in scanned


def test_a_link_cycle_terminates_instead_of_walking_forever(built):
    """Following links makes a cycle reachable; os.walk has no loop detection.
    On Windows this is already reachable today through a junction, with no
    opt-in at all."""
    loop = built.code_repos_dir / "api" / "loop"
    try:
        import os as _os
        _os.symlink(built.code_repos_dir, loop, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")

    scanned = indexer_mod._scan_corpus(indexer_mod.code_corpus(built))
    assert "api/orders.py" in scanned
    # the cycle contributes no duplicate of the file it loops back to
    assert not [k for k in scanned if k.count("loop") > 1]


def test_the_vault_corpus_does_not_follow_links_out_of_itself(tmp_path, monkeypatch):
    """A note tree is authored in place; quietly following a link out of it
    would index files the user never put in their vault."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    vault.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "leaked.md").write_text(
        "# Leak\n\n## S\n" + "content that is not part of the vault " * 3,
        encoding="utf-8")
    try:
        import os as _os
        _os.symlink(outside, vault / "linked", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this machine")

    cfg = Config(vault=vault, data={"model": "fake"})
    assert indexer_mod.memory_corpus(cfg).follow_links is False
    assert indexer_mod._scan_corpus(indexer_mod.memory_corpus(cfg)) == {}


def test_the_vendored_rule_never_touches_the_markdown_corpora(tmp_path, monkeypatch):
    """Only the code corpus sets the pattern; a vault folder named like a
    release must keep being indexed."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    (vault / "reference-1.2.3").mkdir(parents=True)
    (vault / "reference-1.2.3" / "n.md").write_text(
        "# N\n\n## S\n" + "a real note living in a versioned folder " * 3,
        encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    assert indexer_mod.memory_corpus(cfg).vendored_dir_pattern == ""
    assert "reference-1.2.3/n.md" in indexer_mod._scan_corpus(
        indexer_mod.memory_corpus(cfg))
