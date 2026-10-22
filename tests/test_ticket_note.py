"""`mb note new ticket`: path, numbering, refusal before write, schema, round trip."""
import os

import pytest

from singularity import cli
from singularity.config import Config
from singularity.knowledge import tickets
from singularity.machine import projects as projects_mod

# Cyrillic on purpose (the slug must keep it), written as escapes: the engine-language
# scanner reads literals, and this string is data under test.
PROBA = "\u041f\u0440\u043e\u0431\u0430"


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path / "vault", data={"projects": {
        "drone-detection": {"path": str(tmp_path / "dd"), "ticket_prefix": "DD"},
        "noprefix": {"path": str(tmp_path / "np")},
    }})


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


def _new(*extra, title=PROBA, project="drone-detection"):
    argv = ["note", "new", "ticket", title]
    if project:
        argv += ["--project", project]
    return cli.main(argv + list(extra))


def _files(cfg):
    root = cfg.vault / "tickets"
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def test_creates_ticket_at_the_contract_path(wired, cfg, capsys):
    assert _new() == 0
    path = cfg.vault / "tickets" / "drone-detection" / f"DD-1 - {PROBA}.md"
    assert path.is_file()
    assert capsys.readouterr().out.strip() == str(path)


def test_all_frontmatter_fields_and_defaults(wired, cfg):
    _new()
    fm = tickets.parse_ticket(cfg.vault / f"tickets/drone-detection/DD-1 - {PROBA}.md")["frontmatter"]
    assert fm["type"] == "ticket"
    assert fm["title"] == f"DD-1 {PROBA}"
    assert fm["aliases"] == ["DD-1"]
    assert fm["id"] == "DD-1"
    assert fm["project"] == "drone-detection"
    assert (fm["kind"], fm["status"], fm["priority"]) == ("task", "backlog", "P2")
    assert fm["parent"] is None and fm["assignee"] is None and fm["agent"] is None
    assert fm["blocked_by"] == [] and fm["owns"] == [] and fm["contracts"] == []
    assert fm["created"] == fm["updated"] and len(fm["created"]) == 10


def test_flags_land_in_frontmatter_and_repeatable_flags_repeat(wired, cfg):
    for i in range(1, 6):                      # DD-1..DD-5 so parent/blocked-by resolve
        assert _new(title=f"seed{i}") == 0
    assert _new("--kind", "feature", "--priority", "P1", "--parent", "DD-1",
                "--blocked-by", "DD-4", "--blocked-by", "DD-5",
                "--owns", "utils/a.py", "--owns", "utils/b.py",
                "--assignee", "sonnet") == 0
    fm = tickets.parse_ticket(tickets.find_ticket(cfg.vault, "DD-6"))["frontmatter"]
    assert (fm["kind"], fm["priority"], fm["parent"], fm["assignee"]) == (
        "feature", "P1", "DD-1", "sonnet")
    assert fm["blocked_by"] == ["DD-4", "DD-5"]
    assert fm["owns"] == ["utils/a.py", "utils/b.py"]


def test_body_has_the_four_sections_in_order(wired, cfg):
    _new()
    text = next(iter(_files(cfg))).read_text(encoding="utf-8")
    heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert heads == ["## Goal", "## Acceptance criteria", "## Feed", "## Verdict"]


def test_numbering_is_max_plus_one_not_count_plus_one(wired, cfg):
    folder = cfg.vault / "tickets" / "drone-detection"
    folder.mkdir(parents=True)
    (folder / "DD-3 - old.md").write_text("x", encoding="utf-8")
    (folder / "DD-7 - older.md").write_text("x", encoding="utf-8")
    assert _new(title="Next") == 0
    assert (folder / "DD-8 - Next.md").is_file()


def test_numbers_are_not_shared_between_projects(wired, cfg, tmp_path):
    cfg.data["projects"]["other"] = {"path": str(tmp_path / "o"), "ticket_prefix": "OT"}
    _new(title="a")
    _new(title="b", project="other")
    assert (cfg.vault / "tickets/other/OT-1 - b.md").is_file()


@pytest.mark.parametrize("project", [None, "general", "personal", "noprefix"])
def test_refused_without_project_or_prefix_and_nothing_written(wired, cfg, capsys, project, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)          # cwd detects no project
    rc = _new(project=project)
    assert rc != 0
    assert _files(cfg) == []
    assert "[mb]" in capsys.readouterr().err


def test_bad_values_are_refused_before_write(wired, cfg):
    assert _new("--kind", "saga") != 0
    assert _new("--priority", "P9") != 0
    assert _new("--blocked-by", "nonsense") != 0
    assert _files(cfg) == []


def test_render_parse_round_trip(tmp_path):
    text = tickets.render_ticket(
        id="SING-12", title='Quote " and: colon', project="singularity", kind="bug",
        status="blocked", priority="P0", parent="SING-1", blocked_by=["SING-2", "SING-3"],
        owns=["a/b.py"], contracts=["tickets.py API"], assignee="me", agent="abc123",
        created="2026-10-22", updated="2026-10-23", goal="Do the thing.",
        criteria="- [ ] one\n- [ ] two")
    p = tmp_path / "t.md"
    p.write_text(text, encoding="utf-8")
    got = tickets.parse_ticket(p)
    fm = got["frontmatter"]
    assert fm["title"] == 'SING-12 Quote " and: colon'
    assert (fm["id"], fm["kind"], fm["status"], fm["priority"], fm["parent"]) == (
        "SING-12", "bug", "blocked", "P0", "SING-1")
    assert fm["blocked_by"] == ["SING-2", "SING-3"]
    assert fm["owns"] == ["a/b.py"] and fm["contracts"] == ["tickets.py API"]
    assert (fm["assignee"], fm["agent"]) == ("me", "abc123")
    assert (fm["created"], fm["updated"]) == ("2026-10-22", "2026-10-23")
    assert got["sections"][tickets.SECTION_GOAL] == "Do the thing."
    assert got["sections"][tickets.SECTION_CRITERIA] == "- [ ] one\n- [ ] two"
    assert got["sections"][tickets.SECTION_FEED] == ""


def test_parse_rejects_a_non_ticket(tmp_path):
    p = tmp_path / "n.md"
    p.write_text("---\ntype: howto\n---\n\nbody\n", encoding="utf-8")
    with pytest.raises(tickets.TicketError):
        tickets.parse_ticket(p)


def test_write_is_atomic(tmp_path, monkeypatch):
    target = tmp_path / "d" / "DD-1 - x.md"

    def boom(src, dst):
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        tickets.write_ticket(target, "content")
    assert not target.exists()
    assert list(target.parent.iterdir()) == []      # no tmp left behind


def test_write_refuses_to_overwrite(tmp_path):
    target = tmp_path / "t.md"
    target.write_text("keep", encoding="utf-8")
    with pytest.raises(tickets.TicketError):
        tickets.write_ticket(target, "new")
    assert target.read_text(encoding="utf-8") == "keep"


def test_lock_is_released_and_stale_lock_is_broken(wired, cfg):
    _new()
    assert not (cfg.vault / ".mb" / "tickets-drone-detection.lock").exists()
    lock = cfg.vault / ".mb" / "tickets-drone-detection.lock"
    lock.mkdir()
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    assert _new(title="again") == 0


def test_ticket_is_found_by_type_search_filter(wired, cfg):
    from singularity.search.chunking import infer_type, parse_frontmatter
    _new()
    path = next(iter(_files(cfg)))
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    assert infer_type("tickets/drone-detection/" + path.name, fm) == "ticket"


# --- ticket_prefix in the project registry -------------------------------------

def _validate(**prefixes):
    return projects_mod.validate_projects({"projects": {
        pid: {"path": f"/x/{pid}", "ticket_prefix": pf} for pid, pf in prefixes.items()}})


def test_prefix_validation():
    assert _validate(a="DD", b="SING") == {"errors": [], "warnings": []}
    for bad in ("d", "dd", "TOOLONGX", "D1", 5):
        assert _validate(a=bad)["errors"], bad
    assert _validate(a="DD", b="DD")["errors"]


def test_tickets_root_is_declared_everywhere_it_must_be():
    from singularity import config, doctor
    from singularity.machine import vault_guard
    assert "tickets" in vault_guard.MANAGED_ROOTS
    assert "tickets" in config.MEMORY_FOLDERS
    assert "tickets" in doctor.MEMORY_FOLDERS
