"""W08: `mb curator gaps` — the research agenda.

Every signal must be true by construction; anything statistical stays behind
--heuristic. The report is also required to be honest about what it did not
show, which is why the per-signal ceiling reports its own truncation.
"""
from __future__ import annotations

import json

import pytest

from singularity import curator, gaps, sources
from singularity.cli import main
from singularity.config import Config

NOW = 1785000000.0          # fixed clock: age-based scoring must be testable
DAY = 86400.0


@pytest.fixture
def vault(tmp_path):
    (tmp_path / ".mb").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _cfg(vault, **data):
    return Config(vault=vault, data=data)


def _note(vault, rel, body="body text here", fm="", mtime=None):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntype: reference\ntitle: \"{p.stem}\"\ncreated: 2026-01-01\n"
                 f"{fm}---\n\n# {p.stem}\n\n{body}\n", encoding="utf-8")
    if mtime is not None:
        import os
        os.utime(p, (mtime, mtime))
    return p


def _signals(result, name):
    return [i for i in result["items"] if i["signal"] == name]


# --- unprocessed sources ----------------------------------------------------

def test_new_source_is_a_gap(vault):
    cfg = _cfg(vault)
    sid = sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref")["source_id"]
    items = _signals(gaps.report(cfg, now=NOW), "unprocessed_sources")
    assert [i["key"] for i in items] == [sid]
    assert items[0]["detail"]["reason"] == "never processed"


def test_a_full_ingest_cycle_clears_the_signal(vault):
    """Acceptance: unprocessed_sources == 0 right after ingest + mark."""
    cfg = _cfg(vault)
    _note(vault, "reference/derived.md", mtime=NOW)
    sid = sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref",
                      now="2026-01-01T00:00:00")["source_id"]
    sources.attach_notes(cfg, sid, ["reference/derived.md"], status="processed",
                         log=True)
    assert _signals(gaps.report(cfg, now=NOW), "unprocessed_sources") == []


def test_a_source_newer_than_its_notes_is_a_gap_again(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/derived.md", mtime=NOW - 400 * DAY)
    sid = sources.add(cfg, "# Doc\n\nexternal body", origin_ref="ref",
                      now="2026-07-01T00:00:00")["source_id"]
    sources.attach_notes(cfg, sid, ["reference/derived.md"], status="processed")
    items = _signals(gaps.report(cfg, now=NOW), "unprocessed_sources")
    assert len(items) == 1 and "newer" in items[0]["detail"]["reason"]


def test_no_registry_means_no_source_gaps(vault):
    assert _signals(gaps.report(_cfg(vault), now=NOW), "unprocessed_sources") == []


# --- missing pages ----------------------------------------------------------

def test_one_inbound_link_is_not_a_missing_page(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body="see [[Nonexistent Page]]", mtime=NOW)
    assert _signals(gaps.report(cfg, now=NOW), "missing_pages") == []


def test_two_inbound_links_make_a_missing_page(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body="see [[Nonexistent Page]]", mtime=NOW)
    _note(vault, "reference/b.md", body="also [[Nonexistent Page]]", mtime=NOW)
    items = _signals(gaps.report(cfg, now=NOW), "missing_pages")
    assert [i["key"] for i in items] == ["Nonexistent Page"]
    assert items[0]["detail"]["inbound"] == 2


def test_threshold_is_configurable(vault):
    cfg = _cfg(vault, gap_link_min_inbound=1)
    _note(vault, "reference/a.md", body="see [[Nonexistent Page]]", mtime=NOW)
    assert len(_signals(gaps.report(cfg, now=NOW), "missing_pages")) == 1


def test_resolvable_links_are_never_missing_pages(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/target.md", mtime=NOW)
    _note(vault, "reference/a.md", body="see [[target]]", mtime=NOW)
    _note(vault, "reference/b.md", body="see [[target]]", mtime=NOW)
    assert _signals(gaps.report(cfg, now=NOW), "missing_pages") == []


# --- open questions ---------------------------------------------------------

@pytest.mark.parametrize("heading", ["Open questions", "Открытые вопросы",
                                     "TODO", "OPEN: что дальше"])
def test_open_question_sections_are_found(vault, heading):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body=f"## {heading}\n\nчто делать?", mtime=NOW)
    items = _signals(gaps.report(cfg, now=NOW), "open_questions")
    assert any(heading in i["key"] for i in items)


def test_frontmatter_status_open_is_found(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", fm="status: open\n", mtime=NOW)
    items = _signals(gaps.report(cfg, now=NOW), "open_questions")
    assert items and items[0]["detail"]["where"] == "frontmatter status: open"


def test_ordinary_sections_are_not_open_questions(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body="## Контекст\n\nтекст", mtime=NOW)
    assert _signals(gaps.report(cfg, now=NOW), "open_questions") == []


# --- undigested learnings ---------------------------------------------------

def test_old_unmarked_learning_sections_are_gaps(vault):
    cfg = _cfg(vault)
    p = vault / "learnings" / "active" / "T.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: learning\ntitle: T\ncreated: 2026-01-01\n---\n\n# T\n\n"
                 "## 2020-01-01 10:00 — старое\nинсайт\n", encoding="utf-8")
    items = _signals(gaps.report(cfg, now=NOW), "undigested_learnings")
    assert len(items) == 1 and items[0]["detail"]["file"] == "learnings/active/T.md"


def test_recent_learning_sections_are_not_gaps(vault):
    from datetime import datetime
    cfg = _cfg(vault)
    recent = datetime.fromtimestamp(NOW - 2 * DAY).strftime("%Y-%m-%d")
    p = vault / "learnings" / "active" / "T.md"
    p.parent.mkdir(parents=True)
    p.write_text(f"---\ntype: learning\ntitle: T\ncreated: 2026-01-01\n---\n\n# T\n\n"
                 f"## {recent} 10:00 — свежее\nинсайт\n", encoding="utf-8")
    assert _signals(gaps.report(cfg, now=NOW), "undigested_learnings") == []


def test_digest_marked_sections_are_not_gaps(vault):
    from singularity import digest
    cfg = _cfg(vault)
    p = vault / "learnings" / "active" / "T.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: learning\ntitle: T\ncreated: 2026-01-01\n---\n\n# T\n\n"
                 "## 2020-01-01 10:00 — старое\nинсайт\n", encoding="utf-8")
    digest.mark(cfg, [("learnings/active/T.md", "2020-01-01 10:00 — старое")])
    assert _signals(gaps.report(cfg, now=NOW), "undigested_learnings") == []


# --- unlinked from index ----------------------------------------------------

def test_unlinked_does_not_duplicate_orphans(vault):
    """Acceptance: a note with NO links at all is an orphan (curator.analyze
    already reports it) and must not appear here as well."""
    cfg = _cfg(vault)
    _note(vault, "00_INDEX.md", body="see [[mapped]]", mtime=NOW)
    _note(vault, "reference/mapped.md", mtime=NOW)
    _note(vault, "reference/lonely.md", mtime=NOW)                     # orphan
    _note(vault, "reference/connected.md", body="see [[mapped]]", mtime=NOW)

    result = gaps.report(cfg, now=NOW)
    keys = {i["key"] for i in _signals(result, "unlinked_from_index")}
    assert "reference/connected.md" in keys
    assert "reference/mapped.md" not in keys, "reachable from the index"
    assert "reference/lonely.md" not in keys, "that is an orphan, not unlinked"
    assert "reference/lonely.md" in curator.analyze(cfg)["orphans"]


def test_moc_notes_count_as_map_roots(vault):
    cfg = _cfg(vault)
    _note(vault, "_moc/map.md", body="see [[mapped]]", mtime=NOW)
    _note(vault, "reference/mapped.md", body="see [[map]]", mtime=NOW)
    assert _signals(gaps.report(cfg, now=NOW), "unlinked_from_index") == []


def test_learnings_are_never_unlinked_gaps(vault):
    cfg = _cfg(vault)
    _note(vault, "00_INDEX.md", body="nothing", mtime=NOW)
    p = vault / "learnings" / "active" / "T.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\ntype: learning\ntitle: T\ncreated: 2026-01-01\n---\n\n# T\n\n"
                 "see [[00_INDEX]]\n", encoding="utf-8")
    keys = {i["key"] for i in _signals(gaps.report(cfg, now=NOW), "unlinked_from_index")}
    assert not any(k.startswith("learnings/") for k in keys)


# --- decisions without review -----------------------------------------------

def test_old_decision_without_review(vault):
    cfg = _cfg(vault)
    _note(vault, "decisions/old.md", fm="type: decision\nowner: user\nsource: x\n",
          mtime=NOW - 400 * DAY)
    (vault / "decisions" / "old.md").write_text(
        '---\ntype: decision\ntitle: "old"\ncreated: 2020-01-01\nowner: user\n'
        'source: x\n---\n\n# old\n\nbody\n', encoding="utf-8")
    items = _signals(gaps.report(cfg, now=NOW), "decisions_without_review")
    assert [i["key"] for i in items] == ["decisions/old.md"]


def test_reviewed_or_superseded_decisions_are_clean(vault):
    cfg = _cfg(vault)
    for name, extra in (("reviewed.md", "reviewed: 2026-01-01\n"),
                        ("superseded.md", "superseded_by: other\n"),
                        ("archived.md", "status: superseded\n")):
        (vault / "decisions").mkdir(parents=True, exist_ok=True)
        (vault / "decisions" / name).write_text(
            f'---\ntype: decision\ntitle: "{name}"\ncreated: 2020-01-01\n{extra}'
            f'---\n\n# {name}\n\nbody\n', encoding="utf-8")
    assert _signals(gaps.report(cfg, now=NOW), "decisions_without_review") == []


def test_recent_decisions_are_clean(vault):
    from datetime import datetime
    cfg = _cfg(vault)
    recent = datetime.fromtimestamp(NOW - 10 * DAY).strftime("%Y-%m-%d")
    (vault / "decisions").mkdir(parents=True)
    (vault / "decisions" / "new.md").write_text(
        f'---\ntype: decision\ntitle: "new"\ncreated: {recent}\n---\n\n# new\n\nbody\n',
        encoding="utf-8")
    assert _signals(gaps.report(cfg, now=NOW), "decisions_without_review") == []


# --- ranking, ceiling, determinism ------------------------------------------

def test_ranking_is_weight_times_age(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body="## Open questions\n\nq?", mtime=NOW)
    sources.add(cfg, "# Doc\n\nbody", origin_ref="ref", now="2026-01-01T00:00:00")
    result = gaps.report(cfg, now=NOW)
    scores = [i["score"] for i in result["items"]]
    assert scores == sorted(scores, reverse=True)


def test_report_is_deterministic(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body="see [[Ghost]]", mtime=NOW)
    _note(vault, "reference/b.md", body="see [[Ghost]]", mtime=NOW)
    a = json.dumps(gaps.report(cfg, now=NOW), ensure_ascii=False, sort_keys=True)
    b = json.dumps(gaps.report(cfg, now=NOW), ensure_ascii=False, sort_keys=True)
    assert a == b


def test_per_signal_ceiling_reports_what_it_hid(vault):
    cfg = _cfg(vault)
    _note(vault, "00_INDEX.md", body="nothing", mtime=NOW)
    for i in range(15):
        _note(vault, f"reference/n{i:02d}.md", body="see [[00_INDEX]]", mtime=NOW)
    result = gaps.report(cfg, now=NOW, per_signal=3)
    assert len(_signals(result, "unlinked_from_index")) == 3
    assert result["counts"]["unlinked_from_index"] == 15
    assert result["truncated"]["unlinked_from_index"] == 12

    full = gaps.report(cfg, now=NOW, per_signal=0)
    assert len(_signals(full, "unlinked_from_index")) == 15
    assert full["truncated"] == {}


def test_top_limits_the_final_list(vault):
    cfg = _cfg(vault)
    _note(vault, "reference/a.md", body="## Open questions\n\nq?", mtime=NOW)
    _note(vault, "reference/b.md", body="## TODO\n\nq?", mtime=NOW)
    assert len(gaps.report(cfg, now=NOW, top=1)["items"]) == 1


# --- heuristic --------------------------------------------------------------

def test_heuristic_is_off_by_default(vault):
    cfg = _cfg(vault)
    for i in range(3):
        _note(vault, f"reference/n{i}.md", body="## Совершенно Новый Термин\n\ntext",
              mtime=NOW)
    default = gaps.report(cfg, now=NOW)
    assert default["heuristic"] is False
    assert not any(i["detail"].get("heuristic") for i in default["items"])

    with_guess = gaps.report(cfg, now=NOW, heuristic=True)
    assert with_guess["heuristic"] is True
    assert any(i["detail"].get("heuristic") for i in with_guess["items"])


# --- CLI --------------------------------------------------------------------

def test_cli_gaps_json_and_human(vault, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(vault))
    _note(vault, "reference/a.md", body="## Open questions\n\nq?", mtime=NOW)

    assert main(["curator", "gaps", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["open_questions"] >= 1

    assert main(["curator", "gaps"]) == 0
    out = capsys.readouterr().out
    assert "mb curator gaps" in out and "--heuristic" in out


def test_bare_curator_still_works_with_gaps_registered(vault, capsys, monkeypatch):
    monkeypatch.setenv("MB_HOME", str(vault))
    _note(vault, "reference/a.md", mtime=NOW)
    assert main(["curator", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "gaps" not in payload, "gaps must stay a separate subcommand"
