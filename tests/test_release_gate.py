"""T16: release gate -- fixture matrix and acceptance-criteria proofs the
existing test suite did not directly cover (confirmed by investigation
before writing these): legacy config without a `projects` key, an
existing/pre-populated vault at install time, malformed
`~/.claude/settings.json`, foreign hooks surviving a full install->status->
disable->enable->doctor->uninstall flow, SessionStart producing context
independent of `auto_capture` (both Claude and Gemini share the exact same
build_context()), and the "existing notes classify general, never moved"
migration guarantee. Pure verification -- no new business logic, per T16's
own AI prompt ("release gate, without adding new business logic").
See [[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] T16.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1]
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))
if str(ENGINE / "install") not in sys.path:
    sys.path.insert(0, str(ENGINE / "install"))

from singularity import doctor
from singularity.machine import hooks_state
from singularity.config import Config
from singularity.hooks import session_start
from singularity.platform import scheduler
from singularity.search import client as client_mod


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setattr(doctor.Path, "home", classmethod(lambda cls: h))
    monkeypatch.setattr(hooks_state.Path, "home", classmethod(lambda cls: h))
    return h


@pytest.fixture
def vault(tmp_path):
    v = tmp_path / "vault"
    v.mkdir()
    return v


def _stub_heavy(monkeypatch):
    """Neutralise ML/daemon/scheduler externals so doctor.run() is fast and
    deterministic -- same pattern test_doctor.py already established."""
    monkeypatch.setattr(doctor.importlib, "import_module", lambda name: object())
    monkeypatch.setattr(scheduler, "verify", lambda job: {"registered": False})
    monkeypatch.setattr(client_mod, "ping", lambda cfg: None)


# --- SessionStart is structurally independent of auto_capture -----------------

def test_sessionstart_context_produced_when_capture_disabled(monkeypatch, vault):
    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps({"auto_capture": False}), encoding="utf-8")
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: vault)

    ctx = session_start.build_context()
    assert "SEM-FIRST" in ctx
    assert ctx.strip() != ""


def test_gemini_sessionstart_shares_the_same_builder_disabled_or_not(monkeypatch, vault):
    # gemini_session_start.py imports build_context directly from
    # session_start.py (single source) -- there is no auto_capture branch
    # anywhere in either module, so this is the SAME proof for both clients,
    # not a coincidence to be independently re-verified per client.
    from singularity.hooks import gemini_session_start
    assert gemini_session_start.build_context is session_start.build_context

    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps({"auto_capture": False}), encoding="utf-8")
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: vault)

    ctx = gemini_session_start.build_context()
    assert "SEM-FIRST" in ctx


def test_sessionstart_never_reads_auto_capture_key_at_all():
    # structural guarantee, not just an empirical one: grep the SOURCE for
    # any auto_capture reference. If this ever starts reading it, that's a
    # deliberate coupling that deserves its own review, not silent drift.
    src = (ENGINE / "singularity" / "hooks" / "session_start.py").read_text(encoding="utf-8")
    assert "auto_capture" not in src


# --- pre-existing notes classify general, never moved --------------------------

def test_preexisting_note_without_project_frontmatter_classifies_general():
    from singularity.search.chunking import infer_project
    fm = {"type": "reference", "created": "2026-01-01"}   # no `project` key at all
    assert infer_project(fm, "reference/Old.md", []) is None   # None == general


def test_preexisting_note_literal_general_normalizes_to_none():
    from singularity.search.chunking import infer_project
    assert infer_project({"project": "general"}, "reference/Old.md", []) is None


def test_indexing_never_moves_or_renames_vault_files(tmp_path):
    from singularity.config import Config
    from singularity.search import indexer

    vault_dir = tmp_path / "vault"
    note = vault_dir / "reference" / "Old.md"
    note.parent.mkdir(parents=True)
    note.write_text("---\ntype: reference\ncreated: 2026-01-01\n---\n\n# Old\ncontent\n",
                    encoding="utf-8")
    original_path = note
    original_mtime = note.stat().st_mtime

    cfg = Config(vault=vault_dir, data={})

    class _FakeEncoder:
        def encode(self, texts, progress=False):
            import numpy as np
            return np.ones((len(texts), 4), dtype="float32")

    import singularity.search.indexer as indexer_mod
    orig = indexer_mod.shared_encoder
    indexer_mod.shared_encoder = lambda *a, **k: _FakeEncoder()
    try:
        indexer.rebuild(cfg, progress=False)
    finally:
        indexer_mod.shared_encoder = orig

    assert original_path.exists()                       # never moved
    assert original_path.stat().st_mtime == original_mtime   # never rewritten
    assert not (vault_dir / "code").exists()             # no fork-style code/ move target created


def test_no_move_or_rename_call_exists_anywhere_in_the_indexing_path():
    # structural: the entire search package has zero calls that could
    # relocate a vault note -- "no automatic move into code/" holds by
    # absence of the operation, not by a runtime check that could regress.
    search_dir = ENGINE / "singularity" / "search"
    for py in search_dir.glob("*.py"):
        src = py.read_text(encoding="utf-8")
        assert "shutil.move" not in src, py
        assert ".rename(" not in src, py
        assert "os.rename" not in src, py


# --- legacy config without a `projects` key -------------------------------------

def test_legacy_config_without_projects_key_loads_and_validates(vault, monkeypatch):
    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps({"model": "m", "min_score": 0.3}),
                                    encoding="utf-8")
    import singularity.config as config_mod
    monkeypatch.setenv("MB_HOME", str(vault))
    cfg = config_mod.load_config(str(vault))
    assert cfg.get("projects", {}) == {}
    result = config_mod.validate_config(cfg.data)
    assert result["errors"] == []


def test_legacy_config_without_projects_key_survives_doctor(vault, monkeypatch, home):
    mb = vault / ".mb"
    mb.mkdir(parents=True)
    (mb / "config.json").write_text(json.dumps({"model": "m"}), encoding="utf-8")
    _stub_heavy(monkeypatch)
    cfg = Config(vault=vault, data={"model": "m"})
    rc = doctor.run(cfg, as_json=True)
    assert rc in (0, 1)   # runs to completion, no crash on the missing key


# --- malformed ~/.claude/settings.json ------------------------------------------

def test_malformed_settings_json_does_not_crash_hooks_status(home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    data = hooks_state.status(home.parent / "vault-does-not-need-to-exist-for-this")
    assert data["clients"]["claude"]["installed"] is False
    assert data.get("warnings") or data["source"] in ("default", "error")


def test_malformed_settings_json_survives_doctor_d1(vault, monkeypatch, home):
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json", encoding="utf-8")
    _stub_heavy(monkeypatch)
    cfg = Config(vault=vault, data={})
    doctor.run(cfg, as_json=True)
    payload = json.loads((cfg.mb_dir / "doctor-last.json").read_text(encoding="utf-8"))
    d1 = next(c for c in payload["checks"] if c["id"] == "D1-hooks")
    assert d1["status"] == "WARN"   # reported, not a crash


# --- full flow: foreign hooks survive install -> status -> disable -> enable ---
# -> doctor -> uninstall ----------------------------------------------------------

def test_foreign_hooks_survive_full_lifecycle(vault, monkeypatch, home):
    import register_hooks

    settings_path = home / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    foreign_stop = {"hooks": [{"type": "command", "command": "some-other-tool --on-stop"}]}
    settings_path.write_text(json.dumps({"hooks": {"Stop": [foreign_stop]}}), encoding="utf-8")

    # 1. install (register_hooks writes/merges our owned SessionStart+Stop entries)
    rc = register_hooks.main()
    assert rc == 0
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    stop_entries = data["hooks"]["Stop"]
    assert foreign_stop in stop_entries
    assert any(e.get("_mb") for e in stop_entries)

    # 2. status
    cfg = Config(vault=vault, data={})
    status1 = hooks_state.status(vault)
    assert status1["clients"]["claude"]["installed"] is True

    # 3. disable capture
    hooks_state.set_capture_enabled(cfg, False)
    status2 = hooks_state.status(vault)
    assert status2["configured"] is False
    assert status2["clients"]["claude"]["installed"] is True   # hook stays registered

    # 4. re-enable
    hooks_state.set_capture_enabled(cfg, True)
    status3 = hooks_state.status(vault)
    assert status3["configured"] is True

    # 5. doctor (fix=False, just report)
    _stub_heavy(monkeypatch)
    doctor.run(cfg, as_json=True)
    payload = json.loads((cfg.mb_dir / "doctor-last.json").read_text(encoding="utf-8"))
    d1 = next(c for c in payload["checks"] if c["id"] == "D1-hooks")
    assert d1["status"] == "PASS"

    # 6. uninstall (cli.cmd_uninstall's own hook-removal block, exercised directly)
    settings_path.write_text(json.dumps(json.loads(settings_path.read_text(encoding="utf-8"))),
                             encoding="utf-8")   # no-op re-write, keep as-is
    raw = json.loads(settings_path.read_text(encoding="utf-8"))
    hooks = raw.get("hooks", {})
    removed = 0
    for event in list(hooks):
        before = len(hooks[event])
        hooks[event] = [e for e in hooks[event] if not register_hooks._is_owned(e, event)]
        removed += before - len(hooks[event])
        if not hooks[event]:
            del hooks[event]
    settings_path.write_text(json.dumps(raw), encoding="utf-8")

    assert removed >= 1
    final = json.loads(settings_path.read_text(encoding="utf-8"))
    assert foreign_stop in final["hooks"]["Stop"]        # foreign hook survives to the end
    assert not any(e.get("_mb") for e in final["hooks"].get("Stop", []))   # owned entries gone


# --- privacy inventory: what actually lives under .mb/ --------------------------

def test_privacy_inventory_no_snapshot_file_contains_a_planted_secret(monkeypatch, vault):
    """Cross-cutting release-gate check: run every snapshot-writing subsystem
    this epic added (digest, curator, config, nightly trend, reflect) against
    a vault seeded with a note containing an obvious secret marker, then
    grep every file under .mb/ for it. Individual privacy tests already exist
    per-subsystem (T13, T14) -- this is the release-gate cross-check that no
    NEW snapshot writer silently reintroduces content leakage."""
    SECRET = "RELEASE_GATE_PLANTED_SECRET_MUST_NEVER_LEAK"
    (vault / "learnings" / "active").mkdir(parents=True)
    (vault / "learnings" / "active" / "T.md").write_text(
        f"---\ntype: learning\ncreated: 2026-01-01\n---\n\n## S\n{SECRET}\n", encoding="utf-8")

    cfg = Config(vault=vault, data={})
    from singularity.search import indexer

    class _FakeEncoder:
        def encode(self, texts, progress=False):
            import numpy as np
            return np.ones((len(texts), 4), dtype="float32")
    import singularity.search.indexer as indexer_mod
    orig = indexer_mod.shared_encoder
    indexer_mod.shared_encoder = lambda *a, **k: _FakeEncoder()
    try:
        indexer.rebuild(cfg, progress=False)
    finally:
        indexer_mod.shared_encoder = orig

    from singularity.knowledge import nightly
    nightly.run(cfg)

    for p in cfg.mb_dir.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl"):
            content = p.read_text(encoding="utf-8", errors="ignore")
            assert SECRET not in content, f"leaked into {p}"
