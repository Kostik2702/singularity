"""C05: nightly / stats / status / doctor know about the code corpus.

The theme of every test here: a corpus nobody switched on must be invisible
and must never be an error, and a corpus that IS on must never be able to fail
`mb doctor` — unlike `.mb/sources/`, `.mb/code_index/` is 100% derived from
files that live in git, so one command recreates it.
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import singularity.search.indexer as indexer_mod
from singularity import doctor, nightly, stats, status
from singularity.config import Config
from singularity.search import client


class FakeEncoder:
    model_name = "fake"

    def encode(self, texts, progress=False):
        return np.ones((len(texts), 4), dtype=np.float32)


@pytest.fixture
def built(tmp_path, monkeypatch):
    """A vault with a real, freshly built code corpus."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    repos = tmp_path / "Project"
    (repos / "api").mkdir(parents=True)
    (repos / "api" / "orders.py").write_text(
        "def create_order(payload):\n"
        '    """Create an order and return its id."""\n'
        "    return payload\n", encoding="utf-8")
    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text(
        "# Note\n\n## Section\n" + "real note content goes here " * 5, encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")
    return cfg


@pytest.fixture
def unconfigured(tmp_path):
    cfg = Config(vault=tmp_path / "vault", data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    return cfg


# --- nightly -----------------------------------------------------------------

def test_nightly_skips_the_code_step_without_a_root(unconfigured):
    out = nightly._step_code_index(unconfigured)
    assert out == {"status": "skipped", "reason": "code_repos_dir not set"}


def test_nightly_skips_a_configured_root_that_does_not_exist(tmp_path):
    cfg = Config(vault=tmp_path / "vault",
                 data={"code_repos_dir": str(tmp_path / "gone")})
    out = nightly._step_code_index(cfg)
    assert out["status"] == "skipped" and "does not exist" in out["reason"]


def test_nightly_updates_the_code_corpus_when_configured(built, monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action="update", files=None, corpus=None:
                        seen.update(action=action, corpus=corpus) or {"chunks": 7})
    out = nightly._step_code_index(built)
    assert seen == {"action": "update", "corpus": "code"}
    assert out == {"status": "ok", "stats": {"chunks": 7}}


def test_nightly_treats_a_held_lock_as_skipped_not_error(built, monkeypatch):
    def boom(*_a, **_k):
        raise client.RemoteIndexError("locked", error_type="IndexLockedError")

    monkeypatch.setattr(client, "run_index", boom)
    assert nightly._step_code_index(built) == {"status": "skipped", "reason": "locked"}


def test_a_failing_code_step_does_not_break_the_chain(built, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("daemon exploded")

    monkeypatch.setattr(client, "run_index", boom)
    result = nightly.run(built)
    assert result["steps"]["code_index"]["status"] == "error"
    assert result["steps"]["digest"]["status"] == "ok"     # chain continued


def test_trend_row_carries_code_chunks(built):
    row = nightly._step_trend(built, "2026-07-29T00:00:00",
                              {"code_index": {"status": "ok",
                                              "stats": {"chunks": 12}}})
    assert row["status"] == "ok"
    line = json.loads((built.mb_dir / nightly.TREND_HISTORY_NAME)
                      .read_text(encoding="utf-8").splitlines()[-1])
    assert line["code_chunks"] == 12


def test_trend_row_tolerates_a_skipped_code_step(built):
    nightly._step_trend(built, "2026-07-29T00:00:00",
                        {"code_index": {"status": "skipped", "reason": "x"}})
    line = json.loads((built.mb_dir / nightly.TREND_HISTORY_NAME)
                      .read_text(encoding="utf-8").splitlines()[-1])
    assert line["code_chunks"] is None


# --- stats -------------------------------------------------------------------

def test_stats_reports_the_code_corpus(built):
    code = stats.compute(built)["code"]
    assert code["configured"] and code["present"]
    assert code["files"] == 1 and code["chunks"] >= 1
    assert code["repos"] == {"api": code["chunks"]}
    assert "function" in code["by_kind"]


def test_stats_stays_quiet_when_the_corpus_is_off(unconfigured):
    code = stats.compute(unconfigured)["code"]
    assert code == {"configured": False, "present": False}


def test_stats_human_output_mentions_code_only_when_configured(built, unconfigured, capsys):
    stats.run(built)
    assert "code          :" in capsys.readouterr().out
    stats.run(unconfigured)
    assert "code          :" not in capsys.readouterr().out


def test_stats_says_configured_but_not_built(tmp_path, capsys):
    repos = tmp_path / "Project"
    repos.mkdir()
    cfg = Config(vault=tmp_path / "vault",
                 data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    code = stats.compute(cfg)["code"]
    assert code["configured"] is True and code["present"] is False


# --- status ------------------------------------------------------------------

def test_status_snapshot_includes_code(built):
    code = status.snapshot(built)["code"]
    assert code["present"] and code["configured"]
    assert code["chunks"] >= 1 and code["age_hours"] is not None


def test_status_marks_an_off_corpus_as_unconfigured(unconfigured):
    assert status.snapshot(unconfigured)["code"] == {"present": False,
                                                     "configured": False}


# --- doctor ------------------------------------------------------------------

def _check(cfg, fix=False, heavy=False):
    rep = doctor.Report(quiet=True)
    doctor.code_index_block(rep, cfg, fix=fix, heavy=heavy)
    return next(c for c in rep.checks if c["id"] == "K1-code-index")


def test_doctor_skips_when_the_corpus_is_off(unconfigured):
    assert _check(unconfigured)["status"] == "SKIP"


def test_doctor_passes_on_a_fresh_corpus(built):
    check = _check(built)
    assert check["status"] == "PASS"


def test_doctor_warns_when_configured_but_never_built(tmp_path):
    repos = tmp_path / "Project"
    repos.mkdir()
    cfg = Config(vault=tmp_path / "vault",
                 data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    check = _check(cfg)
    assert check["status"] == "WARN" and "mb index rebuild --code" in check["fix"]


def test_doctor_warns_when_the_root_is_gone(tmp_path):
    cfg = Config(vault=tmp_path / "vault",
                 data={"code_repos_dir": str(tmp_path / "vanished")})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    assert _check(cfg)["status"] == "WARN"


def test_doctor_warns_on_a_corpus_built_from_zero_files(tmp_path, monkeypatch):
    """Issue #13: `PASS: 0 chunk(s) from 0 file(s)` is a status contradicting
    its own message. This is the check that should have caught the stale
    daemon-config bug; instead it certified the broken state as healthy."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    repos = tmp_path / "Project"
    repos.mkdir()                                   # exists, but holds no code
    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text("# Note\n\n## S\n" + "content " * 20,
                                          encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")

    check = _check(cfg)

    assert check["status"] == "WARN"
    assert "0 files" in check["detail"]
    assert "code_repos_dir" in check["fix"] and "code_skip_dirs" in check["fix"]


def test_status_marks_an_empty_code_corpus(tmp_path, monkeypatch, capsys):
    """`mb status` printed the empty count in the same neutral tone as a
    healthy one."""
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    repos = tmp_path / "Project"
    repos.mkdir()
    vault = tmp_path / "vault"
    (vault / "howto").mkdir(parents=True)
    (vault / "howto" / "a.md").write_text("# Note\n\n## S\n" + "content " * 20,
                                          encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer_mod.rebuild(cfg, progress=False)
    indexer_mod.rebuild(cfg, progress=False, corpus="code")

    status.run(cfg)

    out = capsys.readouterr().out
    assert "code: 0 chunks from 0 files" in out
    assert "EMPTY" in out


def test_doctor_warns_on_a_stale_corpus(built, monkeypatch):
    built.data["code_index_max_age_days"] = 1
    real = doctor.time.time
    monkeypatch.setattr(doctor.time, "time", lambda: real() + 10 * 86400)
    check = _check(built)
    assert check["status"] == "WARN" and "days old" in check["detail"]


def test_doctor_warns_on_a_model_mismatch(built):
    _, meta_path = indexer_mod.index_files(built.code_index_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["model"] = "some-other-model"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (meta_path.with_name(indexer_mod.SUMMARY_FILE)).unlink()
    check = _check(built)
    assert check["status"] == "WARN" and "different models" in check["detail"]


def test_doctor_fix_heavy_builds_the_corpus(tmp_path, monkeypatch):
    repos = tmp_path / "Project"
    (repos / "api").mkdir(parents=True)
    (repos / "api" / "a.py").write_text("def handler():\n    return 'ok, long enough'\n",
                                        encoding="utf-8")
    cfg = Config(vault=tmp_path / "vault",
                 data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(indexer_mod, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    monkeypatch.setattr(client, "run_index",
                        lambda cfg_, action="update", files=None, corpus=None:
                        indexer_mod.rebuild(cfg_, progress=False, corpus=corpus))
    assert _check(cfg, fix=True, heavy=True)["status"] == "FIXED"
    assert indexer_mod.index_summary(cfg.code_index_dir) is not None


def test_plain_fix_does_not_start_a_multi_minute_build(tmp_path, monkeypatch):
    """`mb doctor --fix` is expected to be quick; embedding a whole repositories
    root is not. Gated behind --heavy exactly like the model/index repairs."""
    repos = tmp_path / "Project"
    repos.mkdir()
    cfg = Config(vault=tmp_path / "vault",
                 data={"model": "fake", "code_repos_dir": str(repos)})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    called = []
    monkeypatch.setattr(client, "run_index",
                        lambda *a, **k: called.append(1) or {})
    assert _check(cfg, fix=True, heavy=False)["status"] == "WARN"
    assert called == []


@pytest.mark.parametrize("scenario", ["off", "unbuilt", "stale-root", "built"])
def test_doctor_never_fails_because_of_code(tmp_path, monkeypatch, built, scenario):
    """FAIL would make `mb doctor` exit 1 over a corpus that one command
    recreates. Block W (raw sources) is the strict one; this is its mirror."""
    if scenario == "built":
        cfg = built
    else:
        data = {"model": "fake"}
        if scenario == "unbuilt":
            (tmp_path / "P").mkdir(exist_ok=True)
            data["code_repos_dir"] = str(tmp_path / "P")
        elif scenario == "stale-root":
            data["code_repos_dir"] = str(tmp_path / "nowhere")
        cfg = Config(vault=tmp_path / "v2", data=data)
        cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    assert _check(cfg)["status"] != "FAIL"


# --- the new config key ------------------------------------------------------

def test_code_index_max_age_days_is_a_known_validated_key():
    from singularity.config import DEFAULT_CONFIG, validate_config

    assert DEFAULT_CONFIG["code_index_max_age_days"] == 7
    assert validate_config({"code_index_max_age_days": 7})["warnings"] == []
    assert validate_config({"code_index_max_age_days": 0})["errors"]
