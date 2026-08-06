"""Four authoring principles, and the surfaces that have to carry them.

Source: https://github.com/multica-ai/andrej-karpathy-skills — CLAUDE.md, four
behavioural guidelines against common LLM coding mistakes. Audited against this
engine on 2026-08-06; two were covered, one was covered at exactly one point,
one was absent.

The audit mattered more than the text. Each principle is delivered where its
decision is made, not collected into a list — a shared list of rules is an
archive ([[Rule - правило звучит в точке принятия решения]]):

  #4 Goal-Driven      already carried by §1 (FINAL GOAL / UNMET if) and §2
                      (plant the violation). Not re-stated; the gap was that
                      following it was expensive, so `tools/plant.py` exists.
  #3 Surgical         was in `coder.md:25` and `web-implementer.md:28` — the
                      two agents that write code — and nowhere else, so the
                      review agents and the main thread never saw it. Lifted to
                      preamble §7. (The first version of this note claimed
                      web-implementer lacked it; review found the copy. The
                      claim was wrong, not the change.)
  #2 Simplicity       had only the config-before-code ladder. Added to §7.
  #1 Think first      collided with "No mid-flight stops" and could not simply
                      be added: whichever text stands at the point wins, so a
                      contradicting bullet next to it would be decoration.
                      Resolved by surface — see the test below.
"""
from pathlib import Path

import pytest

from singularity.machine import rules_delivery
from singularity.machine.hooks_state import THINK_FIRST_FRAGMENT

ENGINE = Path(__file__).resolve().parents[1]
PREAMBLE = (ENGINE / "singularity" / "agents" / "AGENT_SHARED_PREAMBLE.md").read_text(
    encoding="utf-8")


def _section(name: str) -> str:
    """The text of one preamble section, so a marker cannot pass by living in
    a neighbouring section that the agent may never read."""
    start = PREAMBLE.index(f"## {name}")
    end = PREAMBLE.find("\n## ", start + 1)
    return PREAMBLE[start:] if end == -1 else PREAMBLE[start:end]


# --- #3 Surgical Changes ------------------------------------------------------

def test_surgical_changes_reaches_every_code_writing_agent():
    """It lived only in the two agent files that write code (`coder.md:25`,
    `web-implementer.md:28`); the review agents and the main thread never saw
    it. §7 is the section every code-writing agent is told to read."""
    s7 = _section("§7.")
    assert "прослеж" in s7, "§7 must demand every changed line trace to the request"
    assert "не удаляй" in s7 or "назови" in s7, \
        "§7 must say that noticed dead code is reported, not deleted"


@pytest.mark.parametrize("agent", ["coder", "web-implementer"])
def test_the_writing_agents_keep_their_stricter_wording(agent):
    """Lifting the rule to §7 must not quietly delete the places it already
    worked — that would trade coverage for a regression at the two points that
    had it ([[Найденное несоответствие сверить с записанным решением до починки]]).

    Three texts now carry one rule. By the engine's four-surface table an agent
    file MAY hold a stricter role-specific version, so this is legitimate — but
    `rules_delivery.py` states the invariant as "one source, injected… a second
    copy cannot drift", and nothing was binding the three. This test is the
    binding: all three must stand, and §7 must remain the general one."""
    body = (ENGINE / "singularity" / "agents" / f"{agent}.md").read_text(encoding="utf-8")
    assert "No scope expansion" in body, f"{agent} lost its scope gate"
    assert "прослеж" in _section("§7."), "the general rule left §7 while copies remain"


# --- #2 Simplicity First ------------------------------------------------------

def test_simplicity_is_asked_before_done_not_after_review():
    s7 = _section("§7.")
    assert "переусложн" in s7, "§7 must carry the senior-engineer question"
    assert "спекулят" in s7 or "не просили" in s7, \
        "§7 must reject unrequested flexibility/abstraction"


# --- #1 Think Before Coding ---------------------------------------------------

def test_think_first_is_resolved_by_surface_not_bolted_next_to_its_opposite():
    """The reconciliation. My first justification was a who-boundary — subagent
    cannot ask, main thread can — and it was wrong: `No mid-flight stops` also
    stands in `council`, `planning-council` and `observation-council`, which the
    MAIN thread loads and executes. The real axis is time: those texts govern
    work already in flight, this one governs the moment before launch. Both land
    on "questions go before launch", which is why the outcomes never clashed and
    the false reason went unnoticed."""
    f = THINK_FIRST_FRAGMENT
    assert "say them and pick one out loud BEFORE starting" in f, \
        "the instruction must survive, not just the keyword — removing the " \
        "bullet while keeping the words «several readings» passed"

    # Polarity, not vocabulary. Review inverted the carve-out — same words,
    # opposite meaning ("This DOES loosen No mid-flight stops… stop and ask
    # whenever unsure") — and every word-presence assert passed.
    assert "The boundary is WHEN, not who" in f, \
        "the reconciliation must state the axis; a who-boundary is false — " \
        "No mid-flight stops also stands in three council skills the MAIN " \
        "thread executes, so the split cannot be by role"
    assert "once agents are spawned" in f, \
        "the fragment must name what No mid-flight stops actually governs"
    assert "stop and ask" not in f.lower(), \
        "an unconditional 'ask' inverts the rule it claims to reconcile"

    # and gutting the body must fail too: the heading alone is not the rule
    body = f.split("\n", 1)[1]
    assert len(body) > 400, "the fragment was reduced to a heading"


def test_no_mid_flight_stops_survives_unchanged():
    """Control. If the new text had been dropped into §2 instead, the cheap way
    to make it consistent would be to weaken this — which is the failure the
    counter-text rule warns about."""
    s2 = _section("§2.")
    assert "No mid-flight stops" in s2
    assert "не останавливайся спросить" in s2
    assert "ASSUMED" in s2


def test_the_fragment_reaches_every_session(tmp_path, monkeypatch):
    """A constant existing is not the same as the hook emitting it."""
    from singularity.hooks import session_start

    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    (vault / ".mb" / "config.json").write_text("{}", encoding="utf-8")
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: vault)
    assert THINK_FIRST_FRAGMENT in session_start.build_context(str(vault))


def test_the_fragment_stays_within_the_session_budget():
    from singularity.machine.hooks_state import CAPTURE_HOOKS_FRAGMENT
    assert len(THINK_FIRST_FRAGMENT) < 2 * len(CAPTURE_HOOKS_FRAGMENT)


def test_no_fragment_is_lost_to_truncation(tmp_path, monkeypatch):
    """`session_context_max_lines` caps the payload, and fragments are appended
    in order — so the newest is the first casualty, silently. Review measured
    THINK_FIRST present at the 200-line default and absent at 60."""
    from singularity.hooks import session_start
    from singularity.machine import hooks_state as hs

    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    (vault / ".mb" / "config.json").write_text("{}", encoding="utf-8")
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: vault)

    payload = session_start.build_context(str(vault))
    lines = payload.count("\n") + 1
    missing = [n for n in dir(hs) if n.endswith("_FRAGMENT")
               and getattr(hs, n) not in payload]
    assert missing == [], f"dropped before any project content: {missing}"
    assert lines < 200, (
        f"the payload is {lines} lines against the 200-line default cap; the "
        f"fragments are appended first but nothing guarantees they survive")


# --- the registry knows about all of it ---------------------------------------

@pytest.mark.parametrize("rule", [
    "каждая изменённая строка прослеживается до запроса",
    "минимум кода, ничего спекулятивного",
    "развилку назвать до работы, а не после",
])
def test_each_principle_is_registered(rule):
    """Unregistered delivery is how the four Core Disciplines ended up holding
    the preamble with nothing failing if they vanished."""
    assert any(r == rule for r, _, _, _ in rules_delivery.DELIVERIES), \
        f"{rule!r} is delivered but absent from DELIVERIES"


# --- #4 Goal-Driven: the rule existed, following it was expensive -------------

def test_the_mutation_harness_exists():
    """Existence only. The first version of this test also greped the tool for
    `sha256` and `returncode` — both of which occur in its own docstring and in
    an f-string, so the asserts survived deleting the restore check and
    survived swapping the exit code for a substring match on stdout. Behaviour
    is asserted in `tests/test_plant.py`, which never reads the source
    ([[Rule - сканер исходника не пишет то, что ищет]])."""
    assert (ENGINE / "tools" / "plant.py").is_file()
    assert (ENGINE / "tests" / "test_plant.py").is_file(), \
        "the tool the whole discipline rests on needs behavioural tests"
