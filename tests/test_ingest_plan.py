"""W05: deterministic ingest fan-out.

The value of this command is entirely in its verifiability: every proposed
edit must come with the evidence for it (`why`), the same inputs must produce
the same bytes, and it must refuse rather than guess when the two corpora are
not comparable.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from singularity import ingest_plan, sources
from singularity.config import Config
from singularity.search import indexer


class FakeEncoder:
    """Deterministic 8-dim hash embedding (no torch)."""

    model_name = "fake"

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), 8), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash(t)) % (2**32))
            v = rng.random(8).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return out


SHARED = "векторный поиск индексатор корпус эмбеддинги косинус "


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    for rel, extra in (("reference/search.md", ""),
                       ("reference/indexing.md", ""),
                       ("howto/rebuild.md", ""),
                       ("rules/rule-one.md", "")):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: {rel.split('/')[0].rstrip('s')}\ncreated: 2026-01-01\n"
                     f"title: {rel}\n{extra}---\n\n# {rel}\n\n## Раздел\n" + SHARED * 4,
                     encoding="utf-8")
    (vault / "learnings" / "active").mkdir(parents=True)
    (vault / "learnings" / "active" / "T.md").write_text(
        "---\ntype: learning\ncreated: 2026-01-01\ntitle: T\n---\n\n# T\n\n## Insight\n"
        + SHARED * 4, encoding="utf-8")
    (vault / "_moc").mkdir(parents=True)
    (vault / "_moc" / "map.md").write_text(
        "---\ntype: reference\ncreated: 2026-01-01\ntitle: Map\n---\n\n# Map\n\n## S\n"
        + SHARED * 4, encoding="utf-8")
    c = Config(vault=vault, data={"model": "fake", "ingest_fanout_threshold": -1.0})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


def _ingest(cfg, text=None, **kw):
    kw.setdefault("origin_ref", "D:/docs/a.md")
    body = text or ("# Источник\n\n## Про поиск\n" + SHARED * 5 +
                    "\n\n## Про индексацию\n" + SHARED * 5)
    return sources.add(cfg, body, **kw)


def _build(cfg):
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")


# --- status contract (never raises) -----------------------------------------

def test_unknown_source_is_an_error_not_an_exception(cfg):
    out = ingest_plan.plan(cfg, "deadbeefcafe")
    assert "unknown source_id" in out["error"]
    assert out["touch"] == []


def test_missing_indexes_report_status(cfg):
    sid = _ingest(cfg)["source_id"]
    out = ingest_plan.plan(cfg, sid)
    assert out["index_status"] == "missing"
    assert out["sources_index_status"] == "missing"
    assert out["error"] is None and out["touch"] == []


def test_torn_index_reports_status(cfg):
    sid = _ingest(cfg)["source_id"]
    indexer.rebuild(cfg, progress=False, corpus="sources")
    d = cfg.memory_index_dir
    gen = d / "gen_torn"
    gen.mkdir(parents=True)
    np.save(gen / "embeddings.npy", np.zeros((3, 8), dtype=np.float32), allow_pickle=False)
    (gen / "metadata.json").write_text(json.dumps({"chunks": [{"file": "a"}]}),
                                       encoding="utf-8")
    (d / "CURRENT").write_text("gen_torn", encoding="utf-8")
    assert ingest_plan.plan(cfg, sid)["index_status"] == "torn"


def test_source_not_yet_indexed_is_actionable(cfg):
    sid = _ingest(cfg)["source_id"]
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")
    # a SECOND source added after the corpus build is not in it yet
    second = sources.add(cfg, "# Другой\n\n## X\n" + SHARED * 4, origin_ref="b")
    out = ingest_plan.plan(cfg, second["source_id"])
    assert "mb ingest index" in out["error"]


def test_model_mismatch_refuses_instead_of_returning_noise(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    _, meta_path = indexer.index_files(cfg.sources_index_dir)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["model"] = "another-model"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    (meta_path.parent / indexer.SUMMARY_FILE).unlink()   # force the fallback path
    out = ingest_plan.plan(cfg, sid)
    assert "model mismatch" in out["error"]
    assert out["touch"] == []


def test_kill_switch_refuses_with_the_fix(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    cfg.data["ingest_index_sources"] = False
    out = ingest_plan.plan(cfg, sid)
    assert "ingest_index_sources" in out["error"]


# --- fan-out ----------------------------------------------------------------

def test_plan_proposes_pages_with_evidence(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    out = ingest_plan.plan(cfg, sid)
    assert out["error"] is None and out["touch"]
    for entry in out["touch"]:
        why = entry["why"]
        assert why["source_section"] and why["note_section"], "every touch needs evidence"
        assert isinstance(why["pair_score"], float)
        assert entry["file"] and entry["score"] is not None


def test_plan_is_byte_stable_across_runs(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    first = json.dumps(ingest_plan.plan(cfg, sid), ensure_ascii=False, sort_keys=True)
    second = json.dumps(ingest_plan.plan(cfg, sid), ensure_ascii=False, sort_keys=True)
    assert first == second


def test_scores_are_sorted_descending(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    scores = [t["score"] for t in ingest_plan.plan(cfg, sid)["touch"]]
    assert scores == sorted(scores, reverse=True)


def test_top_k_limits_and_defaults_to_config(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    assert len(ingest_plan.plan(cfg, sid, top_k=2)["touch"]) <= 2
    cfg.data["ingest_fanout_top_k"] = 1
    out = ingest_plan.plan(cfg, sid)
    assert out["top_k"] == 1 and len(out["touch"]) <= 1


def test_threshold_filters(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    assert ingest_plan.plan(cfg, sid, threshold=1.1)["touch"] == []


def test_housekeeping_and_learnings_are_never_targets(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    files = {t["file"] for t in ingest_plan.plan(cfg, sid)["touch"]}
    assert not any(f.startswith(("_moc/", "learnings/", "changelog/", "daily/",
                                 "templates/", ".mb/")) for f in files)


def test_personal_notes_are_never_targets(cfg):
    p = cfg.vault / "reference" / "private.md"
    p.write_text("---\ntype: reference\ncreated: 2026-01-01\ntitle: Private\n"
                 "project: personal\n---\n\n# Private\n\n## S\n" + SHARED * 4,
                 encoding="utf-8")
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    files = {t["file"] for t in ingest_plan.plan(cfg, sid)["touch"]}
    assert "reference/private.md" not in files


def test_incompatible_project_targets_are_excluded(cfg):
    p = cfg.vault / "reference" / "otherproj.md"
    p.write_text("---\ntype: reference\ncreated: 2026-01-01\ntitle: Other\n"
                 "project: projb\n---\n\n# Other\n\n## S\n" + SHARED * 4,
                 encoding="utf-8")
    sid = _ingest(cfg, project="proja")["source_id"]
    _build(cfg)
    files = {t["file"] for t in ingest_plan.plan(cfg, sid)["touch"]}
    assert "reference/otherproj.md" not in files
    assert "reference/search.md" in files, "general targets stay compatible"


def test_personal_source_fans_out_to_nothing(cfg):
    sid = _ingest(cfg, project="personal")["source_id"]
    _build(cfg)
    out = ingest_plan.plan(cfg, sid)
    assert out["touch"] == [] and "personal" in out["skipped"]


# --- summary target ---------------------------------------------------------

def test_summary_target_matches_the_command_it_prints(cfg):
    sid = _ingest(cfg, title="Название: с/плохими*символами")["source_id"]
    _build(cfg)
    st = ingest_plan.plan(cfg, sid)["summary_target"]
    assert st["file"].startswith("reference/") and st["file"].endswith(".md")
    assert st["exists"] is False
    assert f"--from-source {sid}" in st["command"]
    # the proposed path is EXACTLY what cmd_note would create for that title
    from singularity.cli import main
    assert main(["--vault", str(cfg.vault), "note", "new", "reference",
                 "Название: с/плохими*символами", "--from-source", sid]) == 0
    assert (cfg.vault / st["file"]).is_file()


def test_summary_type_is_configurable(cfg):
    sid = _ingest(cfg, title="Howto page")["source_id"]
    _build(cfg)
    st = ingest_plan.plan(cfg, sid, summary_type="howto")["summary_target"]
    assert st["file"] == "howto/Howto page.md" and st["type"] == "howto"


def test_summary_target_reports_an_existing_file(cfg):
    sid = _ingest(cfg, title="search")["source_id"]
    _build(cfg)
    assert ingest_plan.plan(cfg, sid)["summary_target"]["exists"] is True


# --- new pages --------------------------------------------------------------

def test_unresolved_wikilinks_become_new_pages(cfg):
    body = ("# Источник\n\n## Про поиск\n" + SHARED * 4 +
            " ссылка на [[Совершенно Новая Страница]] и на [[search]].")
    sid = _ingest(cfg, text=body)["source_id"]
    _build(cfg)
    pages = ingest_plan.plan(cfg, sid)["new_pages"]
    names = {p["name"] for p in pages}
    assert "Совершенно Новая Страница" in names
    assert "search" not in names, "a link that resolves to an existing note is not a gap"
    assert next(p for p in pages if p["name"] == "Совершенно Новая Страница"
                )["reason"] == "unresolved_wikilink"


def test_source_sections_become_new_pages(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    reasons = {p["name"]: p["reason"] for p in ingest_plan.plan(cfg, sid)["new_pages"]}
    assert reasons.get("Про поиск") == "source_section"


@pytest.mark.parametrize("heading", [
    "4. Карта интеграции с существующим кодом",   # enumerated -> document structure
    "II. Разбор",
    "Контекст", "Решение", "Ссылки", "Open questions", "TODO",
    "`mb ingest add`",                             # code span
    "a | b",                                       # table fragment
    "Это предложение, а не название страницы.",    # ends like a sentence
    "x" * 61,                                      # too long to be a title
    "ab",                                          # too short
    "42",
])
def test_structural_headings_never_become_new_pages(heading):
    assert not ingest_plan._looks_like_page_name(heading)


@pytest.mark.parametrize("heading", [
    "Ingest-цикл", "Corpus abstraction", "Windows-safe slug", "vault guard",
])
def test_entity_like_headings_are_accepted(heading):
    assert ingest_plan._looks_like_page_name(heading)


def test_document_structure_is_filtered_out_of_new_pages(cfg):
    body = ("# Источник\n\n## 1. Введение\n" + SHARED * 4 +
            "\n\n## Контекст\n" + SHARED * 4 +
            "\n\n## Ingest-цикл\n" + SHARED * 4)
    sid = _ingest(cfg, text=body)["source_id"]
    _build(cfg)
    names = {p["name"] for p in ingest_plan.plan(cfg, sid)["new_pages"]}
    assert "Ingest-цикл" in names
    assert "1. Введение" not in names and "Контекст" not in names


def test_new_pages_are_deterministic_and_capped(cfg):
    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    first = ingest_plan.plan(cfg, sid, top_k=2)
    assert len(first["new_pages"]) <= 2
    assert first["new_pages"] == ingest_plan.plan(cfg, sid, top_k=2)["new_pages"]


# --- CLI --------------------------------------------------------------------

def test_cli_plan_json_and_exit_codes(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    from singularity.cli import main

    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    assert main(["ingest", "plan", sid, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source_id"] == sid and "touch" in payload

    assert main(["ingest", "plan", "deadbeefcafe", "--json"]) == 2


def test_cli_plan_human_output_names_the_evidence(cfg, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(cfg.vault))
    from singularity.cli import main

    sid = _ingest(cfg)["source_id"]
    _build(cfg)
    assert main(["ingest", "plan", sid]) == 0
    out = capsys.readouterr().out
    assert "summary:" in out and "why:" in out
