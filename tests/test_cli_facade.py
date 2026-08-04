"""The facade must narrow the surface without moving it.

Two classes of test here. The invariants guard the thing that rots first: a new
command added to `_build_parser()` and never placed on the window or in a group
would be invisible in `mb --help` and reachable only by someone who already
knows it exists. The behaviour tests guard the promise that makes the facade
safe at all — a grouped path and its direct path are the same code.
"""
from __future__ import annotations

import pytest

from singularity import facade
from singularity.cli import _build_parser, main


@pytest.fixture()
def parser():
    return _build_parser()


# --------------------------------------------------------------------------
# invariants: window ∪ groups ≡ parser
# --------------------------------------------------------------------------

def _placed() -> set[str]:
    """Every real command the facade claims to place somewhere."""
    placed = {name for _, names in facade.WINDOW for name in names}
    placed -= set(facade.GROUPS)          # group names are not real commands
    placed |= {member for members in facade.GROUPS.values() for member in members}
    placed |= {member for members in facade.HOSTS.values() for member in members}
    return placed


def test_group_names_are_not_commands(parser):
    """A group is a name that fronts others; a host is a command that also does.
    Mixing them would make `mb curator` ambiguous — report, or listing?"""
    commands = set(facade.parser_commands(parser))
    assert not (set(facade.GROUPS) & commands)
    assert set(facade.HOSTS) <= commands


def test_every_parser_command_is_placed(parser):
    """A command nobody can find from the help is a command nobody uses."""
    assert set(facade.parser_commands(parser)) == _placed()


def test_facade_names_nothing_the_parser_lacks(parser):
    commands = set(facade.parser_commands(parser))
    assert _placed() <= commands


def test_groups_do_not_overlap():
    """One home per command: two homes means guessing which one is canonical."""
    seen: set[str] = set()
    for members in facade.GROUPS.values():
        assert not (seen & set(members))
        seen |= set(members)


def test_window_is_small():
    """The point of the exercise. If this needs raising, raise it deliberately."""
    visible = [name for _, names in facade.WINDOW for name in names]
    assert len(visible) <= 10


# --------------------------------------------------------------------------
# argv rewriting
# --------------------------------------------------------------------------

@pytest.mark.parametrize("given,expected", [
    (["service", "daemon", "status"], ["daemon", "status"]),
    (["service", "index", "rebuild"], ["index", "rebuild"]),
    (["setup", "config", "get", "vault_path"], ["config", "get", "vault_path"]),
    (["setup", "project", "list"], ["project", "list"]),
    (["curator", "digest", "plan"], ["digest", "plan"]),
    (["curator", "nightly"], ["nightly"]),
    (["status", "stats"], ["stats"]),
    (["status", "reflect", "--days", "7"], ["reflect", "--days", "7"]),
])
def test_group_paths_rewrite_onto_real_commands(given, expected):
    plan = facade.resolve(given)
    if expected is None:
        assert plan.action == "group"
    else:
        assert (plan.action, plan.argv) == ("run", expected)


def test_direct_paths_are_untouched():
    """The 1878 existing tests, 33 skills and 3 hooks all call these."""
    for argv in (["daemon", "status"], ["curator", "gaps"], ["config", "set", "a", "b"],
                 ["search", "x", "--json"], ["status"], ["note", "new", "rule", "T"]):
        plan = facade.resolve(argv)
        assert (plan.action, plan.argv) == ("run", argv)


def test_a_host_still_runs_itself():
    """`mb curator` reports; it must not turn into a listing because the facade
    hung digest and nightly under it. Same for a host with flags only."""
    for argv in (["curator"], ["curator", "--json"], ["curator", "contradictions"],
                 ["status"], ["status", "--json"]):
        plan = facade.resolve(argv)
        assert (plan.action, plan.argv) == ("run", argv)


def test_global_options_survive_the_rewrite():
    plan = facade.resolve(["--vault", "/tmp/v", "service", "daemon", "status"])
    assert plan.argv == ["--vault", "/tmp/v", "daemon", "status"]


def test_status_flags_are_not_mistaken_for_reports():
    plan = facade.resolve(["status", "--json"])
    assert (plan.action, plan.argv) == ("run", ["status", "--json"])


def test_unknown_subcommand_names_the_group_not_the_root():
    plan = facade.resolve(["service", "nonsense"])
    assert plan.action == "error"
    assert "mb service" in plan.payload and "nonsense" in plan.payload


def test_bare_mb_greets_instead_of_erroring():
    assert facade.resolve([]).action == "greeting"


def test_help_paths():
    assert facade.resolve(["--help"]).action == "help"
    assert facade.resolve(["help"]).action == "help"
    assert facade.resolve(["help", "--all"]).payload == "all"
    # a command's own help still belongs to argparse
    assert facade.resolve(["search", "--help"]).action == "run"


def test_vault_of_reads_both_spellings():
    assert facade.vault_of(["--vault", "/a", "status"]) == "/a"
    assert facade.vault_of(["--vault=/b", "status"]) == "/b"
    assert facade.vault_of(["status"]) is None


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def test_short_help_fits_one_screen_and_hides_the_tail(parser):
    text = facade.render_help(parser)
    assert len(text.splitlines()) <= 30
    # A command is "on the window" when it has its own indented entry line;
    # being *named* inside a group blurb ("config, projects, hooks,
    # install/uninstall") is the opposite of taking a slot.
    entries = {line.split()[0] for line in text.splitlines()
               if line.startswith("  ") and line.strip()}
    for visible in ("search", "curator", "service", "setup"):
        assert visible in entries
    for hidden in ("uninstall", "selftest", "reflect", "backup", "daemon"):
        assert hidden not in entries


def test_full_help_shows_every_command(parser):
    text = facade.render_help(parser, full=True)
    for name in facade.parser_commands(parser):
        assert name in text, f"{name} missing from `mb help --all`"


def test_help_opens_with_a_usage_line_naming_the_program(parser):
    """What a shim-launched help must show instead of `_launch.py`
    (test_bin_shims relies on it), and a plain CLI convention besides."""
    assert facade.render_help(parser).startswith(f"usage: {parser.prog} <command>")
    assert facade.render_group(parser, "service").startswith(
        f"usage: {parser.prog} service <command>")


def test_help_descriptions_come_from_the_parser(parser):
    """Placement is owned here; prose is owned by the parser. No copies."""
    text = facade.render_help(parser)
    assert facade.parser_commands(parser)["search"] in text


def test_group_listing_shows_subcommands_of_both_declaration_styles(parser):
    text = facade.render_group(parser, "service")
    assert "start" in text and "stop" in text        # positional-choices style
    assert "install" in text                          # add_subparsers style


def test_neighbour_clis_are_named(parser):
    text = facade.render_help(parser)
    for neighbour in ("imgqa", "factory"):
        assert neighbour in text


def test_greeting_reports_index_and_daemon():
    line = facade.render_greeting({"index": {"present": True, "chunks": 42, "age_hours": 1},
                                   "daemon": {"ping": True}})
    assert "42 chunks" in line and "daemon up" in line


def test_greeting_survives_a_broken_snapshot():
    line = facade.render_greeting({})
    assert "daemon down" in line


# --------------------------------------------------------------------------
# end to end through main()
# --------------------------------------------------------------------------

def test_main_renders_help_without_touching_the_vault(capsys):
    assert main(["--help"]) == 0
    assert "Singularity memory brain" in capsys.readouterr().out


def test_main_rejects_an_unknown_group_member(capsys):
    assert main(["setup", "nonsense"]) == 2
    assert "mb setup" in capsys.readouterr().err
