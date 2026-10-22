"""`mb board`: columns, next, show, move (rules 1-6), note, assign, check."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from singularity import cli
from singularity.config import Config
from singularity.knowledge import board as bd
from singularity.knowledge import tickets as tk

ENGINE = Path(__file__).resolve().parents[1]
GOOD = "- [ ] it works\n- [ ] it is tested"


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path / "vault", data={"projects": {
        "p": {"path": str(tmp_path / "p"), "ticket_prefix": "PP"}}})


@pytest.fixture
def wired(monkeypatch, cfg):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: cfg)
    return cfg


def mk(cfg, title="t", *, status="backlog", criteria=GOOD, **kw):
    path = tk.create_ticket(cfg.vault, cfg.data["projects"], "p", title,
                            criteria=criteria, **kw)
    if status != "backlog":
        tk.update_ticket(path, fields={"status": status})
    return path.name.split(" - ")[0]


def board(*argv):
    return cli.main(["board", *argv])


def run_json(capsys, *argv):
    capsys.readouterr()
    rc = board(*argv, "--json")
    return rc, json.loads(capsys.readouterr().out)


def snapshot(cfg):
    return {p: p.read_bytes() for p in (cfg.vault / "tickets").rglob("*.md")}


def refused(capsys, cfg, tid, status, code, *extra):
    before = snapshot(cfg)
    rc, out = run_json(capsys, "move", tid, status, *extra)
    assert rc == 1 and not out["ok"]
    assert code in [v["code"] for v in out["refused"]], out
    assert snapshot(cfg) == before, "a refused move must not change a byte"


# --- views -----------------------------------------------------------------------

def test_board_columns_and_default_view(wired, cfg, capsys):
    a, b = mk(cfg, "a"), mk(cfg, "b", status="ready")
    rc, out = run_json(capsys, "--project", "p")
    assert rc == 0
    cols = out["columns"]
    assert [r["id"] for r in cols["backlog"]] == [a] and [r["id"] for r in cols["ready"]] == [b]
    assert set(cols) == set(tk.STATUSES)


def test_next_orders_by_priority_then_id_and_skips_open_blockers(wired, cfg, capsys):
    blocker = mk(cfg, "blocker", status="in-progress")
    p2a = mk(cfg, "p2a", status="ready")
    p0 = mk(cfg, "p0", status="ready", priority="P0")
    p2b = mk(cfg, "p2b", status="ready")
    p1 = mk(cfg, "p1", status="ready", priority="P1")
    mk(cfg, "waits", status="ready", priority="P0", blocked_by=[blocker])
    rc, out = run_json(capsys, "next")
    assert [r["id"] for r in out["next"]] == [p0, p1, p2a, p2b]
    tk.update_ticket(tk.find_ticket(cfg.vault, blocker), fields={"status": "done"})
    rc, out = run_json(capsys, "next")
    assert [r["id"] for r in out["next"]][:2] == [p0, "PP-6"]


def test_next_never_offers_epics(wired, cfg, capsys):
    mk(cfg, "epic", status="ready", kind="epic", priority="P0")
    task = mk(cfg, "task", status="ready", priority="P3")
    rc, out = run_json(capsys, "next")
    assert [r["id"] for r in out["next"]] == [task]


def test_show(wired, cfg, capsys):
    a = mk(cfg, "a")
    rc, out = run_json(capsys, "show", a)
    assert rc == 0 and out["frontmatter"]["id"] == a and out["sections"][tk.SECTION_CRITERIA] == GOOD
    assert run_json(capsys, "show", "PP-99")[0] == 1


# --- transitions -------------------------------------------------------------------

def test_happy_path_through_every_status(wired, cfg, capsys):
    a = mk(cfg, owns=["hp"])
    for s in ("ready", "in-progress", "review", "in-progress", "review"):
        rc, out = run_json(capsys, "move", a, s)
        assert rc == 0 and out["to"] == s
    assert board("edit", a, "--verdict", "ran the suite: green") == 0
    rc, out = run_json(capsys, "move", a, "done")
    assert rc == 0 and out["to"] == "done"
    assert tk.parse_ticket(tk.find_ticket(cfg.vault, a))["frontmatter"]["status"] == "done"


def test_bad_transition_refused(wired, cfg, capsys):
    a = mk(cfg)
    refused(capsys, cfg, a, "in-progress", "bad_transition")
    refused(capsys, cfg, a, "nonsense", "bad_transition")


def test_blocked_returns_to_previous_status_only(wired, cfg, capsys):
    a = mk(cfg, status="ready")
    assert run_json(capsys, "move", a, "blocked")[0] == 0
    refused(capsys, cfg, a, "review", "bad_transition")
    assert run_json(capsys, "move", a, "ready")[0] == 0
    assert tk.parse_ticket(tk.find_ticket(cfg.vault, a))["frontmatter"]["blocked_from"] is None


# --- rules 1-6 --------------------------------------------------------------------

def test_rule1_open_blocker_refuses_in_progress(wired, cfg, capsys):
    blocker = mk(cfg, "blocker")
    a = mk(cfg, "a", status="ready", blocked_by=[blocker], owns=["r1"])
    refused(capsys, cfg, a, "in-progress", "blocked_open")
    tk.update_ticket(tk.find_ticket(cfg.vault, blocker), fields={"status": "done"})
    assert run_json(capsys, "move", a, "in-progress")[0] == 0


@pytest.mark.parametrize("text", ["", "<!-- fill me -->", "TBD", "- [ ]\n<!-- x -->"])
def test_rule2_empty_criteria_refuse_ready(wired, cfg, capsys, text):
    a = mk(cfg, criteria=text)
    refused(capsys, cfg, a, "ready", "no_criteria")


def test_rule3_wip_limit_and_epics_do_not_count(wired, cfg, capsys):
    cfg.data["board_wip_limit"] = 2
    mk(cfg, "e", status="in-progress", kind="epic")
    mk(cfg, "x", status="in-progress")
    mk(cfg, "y", status="in-progress")
    c = mk(cfg, "c", status="ready", owns=["c3"])
    refused(capsys, cfg, c, "in-progress", "wip_limit")
    epic2 = mk(cfg, "e2", status="ready", kind="epic")
    assert run_json(capsys, "move", epic2, "in-progress")[0] == 0
    cfg.data["projects"]["p"]["wip_limit"] = 3          # per-project override beats the global
    assert run_json(capsys, "move", c, "in-progress")[0] == 0


def test_default_wip_limit_is_four(cfg):
    assert bd.wip_limit(cfg.get, "p") == 4


def test_rule4_owns_overlap(wired, cfg, capsys):
    mk(cfg, "x", status="in-progress", owns=["src/a/"])
    exact = mk(cfg, "exact", status="ready", owns=["./src/a"])
    inside = mk(cfg, "inside", status="ready", owns=["src/a/b.py"])
    sibling = mk(cfg, "sibling", status="ready", owns=["src/ab"])
    nothing = mk(cfg, "nothing", status="ready", kind="spike")
    refused(capsys, cfg, exact, "in-progress", "owns_overlap")
    refused(capsys, cfg, inside, "in-progress", "owns_overlap")
    assert run_json(capsys, "move", sibling, "in-progress")[0] == 0
    assert run_json(capsys, "move", nothing, "in-progress")[0] == 0


def test_rule5_done_only_from_review(wired, cfg, capsys):
    for start in ("backlog", "ready", "in-progress", "blocked"):
        a = mk(cfg, start, status=start)
        refused(capsys, cfg, a, "done", "done_requires_review")
        refused(capsys, cfg, a, "done", "done_requires_review", "--force-reason", "because")


def test_rule6_unknown_refs_refuse_and_cannot_be_forced(wired, cfg, capsys):
    a = mk(cfg, "a", status="ready")
    tk.update_ticket(tk.find_ticket(cfg.vault, a), fields={"blocked_by": ["PP-77"]})
    refused(capsys, cfg, a, "in-progress", "unknown_ref", "--force-reason", "trust me")
    b = mk(cfg, "b", status="ready")
    tk.update_ticket(tk.find_ticket(cfg.vault, b), fields={"parent": "PP-88"})
    refused(capsys, cfg, b, "in-progress", "unknown_ref")


def test_force_reason_bypasses_1_to_4_and_is_recorded(wired, cfg, capsys):
    blocker = mk(cfg, "blocker")
    a = mk(cfg, "a", status="ready", blocked_by=[blocker], owns=["f1"])
    rc, out = run_json(capsys, "move", a, "in-progress", "--force-reason", "lead decision")
    assert rc == 0 and out["forced"] == ["blocked_open"]
    feed = tk.parse_ticket(tk.find_ticket(cfg.vault, a))["sections"][tk.SECTION_FEED]
    assert "lead decision" in feed


# --- note / assign: other sections and `updated` ----------------------------------

def test_note_appends_to_feed_only(wired, cfg, capsys):
    a = mk(cfg, goal="the goal")
    path = tk.find_ticket(cfg.vault, a)
    tk.update_ticket(path, fields={"updated": "2000-01-01"})
    path.write_text(path.read_text(encoding="utf-8").replace(
        "## Verdict\n", "## Verdict\n\nfine\n"), encoding="utf-8")
    before = tk.parse_ticket(path)
    assert board("note", a, "first", "--by", "exec") == 0
    assert board("note", a, "second") == 0
    after = tk.parse_ticket(path)
    for name in (tk.SECTION_GOAL, tk.SECTION_CRITERIA, tk.SECTION_VERDICT):
        assert after["sections"][name] == before["sections"][name]
    lines = after["sections"][tk.SECTION_FEED].splitlines()
    assert len(lines) == 2 and lines[0].endswith("exec: first") and lines[1].endswith("lead: second")
    assert after["frontmatter"]["updated"] != "2000-01-01"
    assert after["sections"][tk.SECTION_VERDICT] == "fine"


def test_assign_sets_fields(wired, cfg, capsys):
    a = mk(cfg)
    assert board("assign", a, "--assignee", "coder", "--agent", "abc") == 0
    fm = tk.parse_ticket(tk.find_ticket(cfg.vault, a))["frontmatter"]
    assert (fm["assignee"], fm["agent"]) == ("coder", "abc")
    assert board("assign", a) == 1


def test_two_parallel_notes_both_land(wired, cfg):
    a = mk(cfg)
    script = ("import sys\nfrom pathlib import Path\n"
              "from singularity.knowledge import board\n"
              "for i in range(15):\n"
              "    board.add_note(Path(sys.argv[1]), sys.argv[2], f'{sys.argv[3]}-{i}')\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(cfg.vault), a, tag],
                              cwd=ENGINE) for tag in ("A", "B")]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    feed = tk.parse_ticket(tk.find_ticket(cfg.vault, a))["sections"][tk.SECTION_FEED]
    assert len(feed.splitlines()) == 30
    for tag in "AB":
        for i in range(15):
            assert f"{tag}-{i}" in feed


def test_note_unknown_ticket(wired, cfg):
    assert board("note", "PP-9", "x") == 1


# --- check --------------------------------------------------------------------------

def test_check_reports_every_invariant(wired, cfg, capsys):
    rc, out = run_json(capsys, "check")
    assert rc == 0 and out["ok"]
    ghost = mk(cfg, "ghost", status="ready")
    tk.update_ticket(tk.find_ticket(cfg.vault, ghost), fields={"blocked_by": ["PP-70"]})
    mk(cfg, "bare", status="in-progress", criteria="")
    mk(cfg, "o1", status="in-progress", owns=["z"])
    mk(cfg, "o2", status="in-progress", owns=["z/q"])
    rc, out = run_json(capsys, "check")
    codes = {v["code"] for v in out["violations"]}
    assert rc == 1 and {"unknown_ref", "no_criteria", "owns_overlap"} <= codes


# --- pure helpers ---------------------------------------------------------------------

def test_owns_collide_segment_boundary():
    assert bd.owns_collide(["a/b"], ["a/b/c"]) and bd.owns_collide(["a/b/"], ["./a/b"])
    assert not bd.owns_collide(["a/b"], ["a/bc"])
    assert not bd.owns_collide([], ["a"])


# --- create with a body, and `mb board edit` ---------------------------------------------

def _new_ticket(*extra, title="Body"):
    return cli.main(["note", "new", "ticket", title, "--project", "p", *extra])


def test_create_with_goal_and_criteria(wired, cfg):
    assert _new_ticket("--goal", "Ship it", "--criteria", "works", "--criteria", "tested") == 0
    got = tk.parse_ticket(tk.find_ticket(cfg.vault, "PP-1"))["sections"]
    assert got[tk.SECTION_GOAL] == "Ship it"
    assert got[tk.SECTION_CRITERIA] == "- [ ] works\n- [ ] tested"
    assert board("move", "PP-1", "ready") == 0          # no --force-reason needed


def test_edit_every_field(wired, cfg, capsys):
    other = mk(cfg, "other")
    a = mk(cfg, "a", goal="old goal")
    assert board("edit", a, "--goal", "new goal", "--criteria", "one", "--criteria", "two",
                 "--add-criterion", "three", "--verdict", "ok", "--title", "Renamed",
                 "--priority", "P0", "--kind", "bug", "--blocked-by", other,
                 "--owns", "x/y", "--by", "me") == 0
    got = tk.parse_ticket(tk.find_ticket(cfg.vault, a))
    fm, sec = got["frontmatter"], got["sections"]
    assert (fm["title"], fm["priority"], fm["kind"]) == (f"{a} Renamed", "P0", "bug")
    assert fm["blocked_by"] == [other] and fm["owns"] == ["x/y"]
    assert sec[tk.SECTION_GOAL] == "new goal" and sec[tk.SECTION_VERDICT] == "ok"
    assert sec[tk.SECTION_CRITERIA] == "- [ ] one\n- [ ] two\n- [ ] three"
    assert f"# {a} Renamed" in got["path"].read_text(encoding="utf-8")
    assert sec[tk.SECTION_FEED].splitlines()[-1].endswith("me: edited: add_criteria, blocked_by, criteria, "
                                                          "goal, kind, owns, priority, title, verdict by me")
    assert board("edit", a, "--remove-blocked-by", other, "--remove-owns", "x/y") == 0
    fm = tk.parse_ticket(tk.find_ticket(cfg.vault, a))["frontmatter"]
    assert fm["blocked_by"] == [] and fm["owns"] == []


def test_edit_cannot_touch_id_status_project(wired, cfg, capsys):
    a = mk(cfg, "a")
    before = snapshot(cfg)
    for key in ("status", "id", "project"):
        with pytest.raises(bd.BoardError):
            bd.edit(cfg.vault, a, {key: "x"})
    with pytest.raises(SystemExit):
        board("edit", a, "--status", "done")
    assert snapshot(cfg) == before


def test_edit_bad_values_and_nothing_to_edit(wired, cfg):
    a = mk(cfg, "a")
    before = snapshot(cfg)
    assert board("edit", a, "--priority", "P9") == 1
    assert board("edit", a, "--kind", "saga") == 1
    assert board("edit", a) == 1
    assert board("edit", a, "--blocked-by", "nonsense") == 1
    assert board("edit", "PP-99", "--goal", "x") == 1
    assert snapshot(cfg) == before


def test_edit_rechecks_refs_and_owns_for_a_ticket_in_progress(wired, cfg, capsys):
    mk(cfg, "x", status="in-progress", owns=["src/a"])
    mine = mk(cfg, "mine", status="in-progress", owns=["lib"])
    before = snapshot(cfg)
    rc, out = run_json(capsys, "edit", mine, "--owns", "src/a/b.py")
    assert rc == 1 and "owns_overlap" in [v["code"] for v in out["refused"]]
    rc, out = run_json(capsys, "edit", mine, "--blocked-by", "PP-88")
    assert rc == 1 and "unknown_ref" in [v["code"] for v in out["refused"]]
    assert snapshot(cfg) == before
    backlog = mk(cfg, "idle", owns=["lib2"])
    assert board("edit", backlog, "--owns", "src/a") == 0      # not in progress: no clash check


def test_edit_keeps_untouched_sections_byte_for_byte(wired, cfg):
    a = mk(cfg, "a", goal="g")
    board("edit", a, "--verdict", "verdict text")
    board("note", a, "n1")
    path = tk.find_ticket(cfg.vault, a)
    before = tk.parse_ticket(path)["sections"]
    assert board("edit", a, "--goal", "changed") == 0
    after = tk.parse_ticket(path)["sections"]
    for name in (tk.SECTION_CRITERIA, tk.SECTION_FEED[:4] and tk.SECTION_VERDICT):
        assert after[name] == before[name]
    assert after[tk.SECTION_VERDICT] == "verdict text" and "n1" in after[tk.SECTION_FEED]
    assert after[tk.SECTION_GOAL] == "changed"


def test_parallel_edits_and_notes_lose_nothing(wired, cfg):
    a = mk(cfg)
    script = ("import sys\nfrom pathlib import Path\n"
              "from singularity.knowledge import board\n"
              "v, tid, tag = Path(sys.argv[1]), sys.argv[2], sys.argv[3]\n"
              "for i in range(10):\n"
              "    board.add_note(v, tid, f'{tag}-note-{i}')\n"
              "    board.edit(v, tid, {'add_criteria': [f'{tag}-crit-{i}']})\n")
    procs = [subprocess.Popen([sys.executable, "-c", script, str(cfg.vault), a, tag],
                              cwd=ENGINE) for tag in ("A", "B")]
    assert [p.wait(timeout=180) for p in procs] == [0, 0]
    got = tk.parse_ticket(tk.find_ticket(cfg.vault, a))
    feed, crit = got["sections"][tk.SECTION_FEED], got["sections"][tk.SECTION_CRITERIA]
    for tag in "AB":
        for i in range(10):
            assert f"{tag}-note-{i}" in feed and f"{tag}-crit-{i}" in crit


# --- SING-21: done needs a Verdict -------------------------------------------------------

def test_done_with_empty_verdict_is_refused_and_forceable(wired, cfg, capsys):
    a = mk(cfg, status="review")
    refused(capsys, cfg, a, "done", "no_verdict")
    rc, out = run_json(capsys, "move", a, "done", "--force-reason", "no check needed")
    assert rc == 0 and out["forced"] == ["no_verdict"]
    feed = tk.parse_ticket(tk.find_ticket(cfg.vault, a))["sections"][tk.SECTION_FEED]
    assert "no check needed" in feed


def test_done_with_a_verdict_passes_and_other_targets_do_not_need_one(wired, cfg, capsys):
    a = mk(cfg, status="review", owns=["v1"])
    assert run_json(capsys, "move", a, "in-progress")[0] == 0
    assert board("edit", a, "--verdict", "checked") == 0
    assert run_json(capsys, "move", a, "review")[0] == 0
    assert run_json(capsys, "move", a, "done")[0] == 0


def test_check_reports_done_without_verdict(wired, cfg, capsys):
    mk(cfg, "bare", status="done")
    rc, out = run_json(capsys, "check")
    assert rc == 1 and "no_verdict" in {v["code"] for v in out["violations"]}
    a = "PP-1"
    board("edit", a, "--verdict", "ok")
    assert run_json(capsys, "check")[0] == 0


# --- SING-23: file-changing kinds need `owns` ----------------------------------------------

@pytest.mark.parametrize("kind", ["feature", "task", "bug", "chore"])
def test_no_owns_refuses_in_progress_for_file_changing_kinds(wired, cfg, capsys, kind):
    a = mk(cfg, kind, status="ready", kind=kind)
    refused(capsys, cfg, a, "in-progress", "no_owns")
    rc, out = run_json(capsys, "move", a, "in-progress", "--force-reason", "scratch work")
    assert rc == 0 and out["forced"] == ["no_owns"]


@pytest.mark.parametrize("kind", ["spike", "review", "epic"])
def test_kinds_without_file_changes_need_no_owns(wired, cfg, capsys, kind):
    a = mk(cfg, kind, status="ready", kind=kind)
    assert run_json(capsys, "move", a, "in-progress")[0] == 0


def test_check_reports_in_progress_without_owns(wired, cfg, capsys):
    mk(cfg, "bare", status="in-progress")
    rc, out = run_json(capsys, "check")
    assert rc == 1 and "no_owns" in {v["code"] for v in out["violations"]}


# --- SING-24: references, id shape, cycles, prefix hint, stale lock ---------------------------

def test_create_refuses_unknown_parent_or_blocker_and_writes_nothing(wired, cfg):
    assert _new_ticket("--parent", "PP-77") == 1
    assert _new_ticket("--blocked-by", "PP-78") == 1
    assert snapshot(cfg) == {}
    first = mk(cfg, "first")
    assert _new_ticket("--parent", first, "--blocked-by", first) == 0


def test_edit_parent_sets_and_refuses_unknown_or_self(wired, cfg, capsys):
    a, b = mk(cfg, "a"), mk(cfg, "b")
    assert board("edit", a, "--parent", b) == 0
    assert tk.parse_ticket(tk.find_ticket(cfg.vault, a))["frontmatter"]["parent"] == b
    before = snapshot(cfg)
    rc, out = run_json(capsys, "edit", a, "--parent", "PP-77")
    assert rc == 1 and "unknown_ref" in [v["code"] for v in out["refused"]]
    assert run_json(capsys, "edit", a, "--parent", a)[0] == 1
    assert run_json(capsys, "edit", a, "--parent", "nonsense")[0] == 1
    assert snapshot(cfg) == before


@pytest.mark.parametrize("bad", ["PP-?", "PP-[1]", "PP-*", "pp-1", "PP-1\n"])
def test_ids_are_validated_before_the_file_search(wired, cfg, bad):
    mk(cfg, "a")
    before = snapshot(cfg)
    assert tk.find_ticket(cfg.vault, bad) is None
    assert board("note", bad, "x") == 1
    assert board("move", bad, "ready") == 1
    assert snapshot(cfg) == before


def test_self_block_and_cycles_are_refused_by_edit(wired, cfg, capsys):
    a, b, c = mk(cfg, "a"), mk(cfg, "b"), mk(cfg, "c")
    before = snapshot(cfg)
    rc, out = run_json(capsys, "edit", a, "--blocked-by", a)
    assert rc == 1 and "blocked_cycle" in [v["code"] for v in out["refused"]]
    assert board("edit", b, "--blocked-by", a) == 0
    assert board("edit", c, "--blocked-by", b) == 0
    rc, out = run_json(capsys, "edit", a, "--blocked-by", c)
    assert rc == 1 and "blocked_cycle" in [v["code"] for v in out["refused"]]
    assert tk.parse_ticket(tk.find_ticket(cfg.vault, a))["frontmatter"]["blocked_by"] == []
    assert snapshot(cfg) != before            # the legal edits did land


def test_check_finds_a_cycle_written_by_hand(wired, cfg, capsys):
    a, b = mk(cfg, "a"), mk(cfg, "b")
    tk.update_ticket(tk.find_ticket(cfg.vault, a), fields={"blocked_by": [b]})
    tk.update_ticket(tk.find_ticket(cfg.vault, b), fields={"blocked_by": [a]})
    rc, out = run_json(capsys, "check")
    assert rc == 1 and "blocked_cycle" in {v["code"] for v in out["violations"]}


def test_prefix_hint_names_the_command_that_works(cfg):
    with pytest.raises(tk.TicketError) as exc:
        tk.require_prefix({"p": {"path": "x"}}, "p")
    assert "mb project set p --ticket-prefix" in str(exc.value)
    assert "config set" not in str(exc.value)


def test_stale_ticket_lock_is_broken_through_the_atomic_breaker(wired, cfg, monkeypatch):
    import os
    calls = []
    real = tk.break_stale_lock
    monkeypatch.setattr(tk, "break_stale_lock", lambda lock, age: calls.append(lock) or real(lock, age))
    a = mk(cfg)
    lock = cfg.vault / ".mb" / f"ticket-{a}.lock"
    lock.mkdir(parents=True)
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    assert board("note", a, "after a dead holder") == 0
    assert calls and not lock.exists()


def test_threads_never_share_a_ticket_lock_even_when_it_starts_stale(wired, cfg):
    import os
    import threading
    a = mk(cfg)
    lock = cfg.vault / ".mb" / f"ticket-{a}.lock"
    lock.mkdir(parents=True, exist_ok=True)
    old = lock.stat().st_mtime - 3600
    os.utime(lock, (old, old))
    inside, overlaps = [], []

    def work():
        with tk._ticket_lock(cfg.vault, a):
            inside.append(1)
            overlaps.append(len(inside))
            inside.pop()

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert overlaps and max(overlaps) == 1
