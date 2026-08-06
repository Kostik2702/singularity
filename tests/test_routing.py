"""The chain of work is announced when a task arrives — and stays quiet otherwise.

These tests guard three separable things, because each failed differently in
the design:

1. The classifier picks a chain and can say which words picked it. A router
   that cannot name its evidence is a guess the reader has no way to correct.
2. It stays SILENT on acknowledgements. A gate that fires on "спасибо" teaches
   its reader to skip it, and is then worse than no gate at all.
3. The phase files it points at live in the ENGINE. They used to sit in
   `{{VAULT}}/workflow/`, which made a ten-phase procedure a property of which
   vault was configured — the dependency v0.13.0 removed for the preamble and
   left standing here.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from singularity.machine import routing

ENGINE = Path(__file__).resolve().parents[1]


# --- classification -----------------------------------------------------------

@pytest.mark.parametrize("prompt,expected", [
    ("почини падающий тест в индексаторе", "dev"),
    ("добавь UserPromptSubmit хук и зарегистрируй его в установщике", "dev"),
    ("разберись почему демон отдаёт старый код", "research"),
    ("проверь код в register_skills, готово к мержу?", "review"),
    ("сделай секцию лендинга с адаптивной вёрсткой", "web"),
    ("убери фон с картинки и проверь ассет", "image"),
    ("запомни что записи в волт идут только через mb", "memory"),
    ("обнови волт из гита", "ops"),
    ("выключи stop hooks", "ops"),
])
def test_a_task_reaches_its_chain(prompt, expected):
    verdict = routing.classify(prompt)
    assert verdict is not None, f"{prompt!r} routed nowhere"
    assert verdict[0] == expected, f"{prompt!r} -> {verdict[0]}, markers {verdict[1]}"


@pytest.mark.parametrize("chatter", ["спасибо", "да", "ок, продолжай", "а почему?",
                                     "ещё раз", "", "   "])
def test_the_router_stays_quiet_on_chatter(chatter):
    assert routing.classify(chatter) is None


def test_a_verdict_always_names_its_evidence():
    """The markers are the whole reason this is a router and not an oracle."""
    key, markers = routing.classify("почини падающий тест в индексаторе")
    assert markers, "a chain was chosen with nothing to point at"
    assert all(m in "почини падающий тест в индексаторе".lower() for m in markers)
    assert all(m in routing.CHAINS[key]["markers"] for m in markers)


def test_specificity_outranks_class_priority():
    """«обнови волт из гита» matches the memory stem «волт» AND the ops phrase
    «обнови волт». The longer marker is the one that recognised the task; the
    bare stem winning on priority sent a git pull down the note-writing chain."""
    key, markers = routing.classify("обнови волт из гита")
    assert key == "ops"
    assert "обнови волт" in markers


def test_a_marker_never_fires_inside_an_unrelated_word():
    """Planted violation, kept: bare «установ» also matches «установщике», which
    routed a writing task to operations. Whole phrases only."""
    assert "установ" not in routing.CHAINS["ops"]["markers"]
    key, _ = routing.classify("зарегистрируй хук в установщике проекта")
    assert key == "dev"


# --- the chains themselves ----------------------------------------------------

def test_every_chain_is_complete():
    for key, chain in routing.CHAINS.items():
        assert chain["title"] and chain["steps"], f"{key}: an empty chain routes nowhere"
        assert chain["note"], f"{key}: a chain without its governing rule is a bare list"
        assert chain["markers"], f"{key}: unreachable — no marker selects it"


def test_every_named_entry_phase_exists_in_the_engine():
    for key, chain in routing.CHAINS.items():
        if chain["entry"] is None:
            continue
        assert routing.phase_file(chain["entry"]) is not None, \
            f"{key} enters at {chain['entry']}, which is not in {routing.WORKFLOW}"


def test_phase_files_ship_with_the_engine_not_the_vault():
    """The move this change exists to make: no vault, still a workflow."""
    assert routing.WORKFLOW.is_dir(), "the engine carries no phase files"
    assert ENGINE in routing.WORKFLOW.parents
    phases = list(routing.WORKFLOW.glob("phase-*.md"))
    assert len(phases) >= 10, f"only {len(phases)} phase files reached the engine"


def test_no_phase_file_addresses_the_vault_for_its_own_flow():
    """A phase that reads its sibling out of `{{VAULT}}` reintroduces exactly
    the dependency being removed."""
    offenders = [p.name for p in routing.WORKFLOW.glob("*.md")
                 if "{{VAULT}}/workflow" in p.read_text(encoding="utf-8")]
    assert offenders == [], f"phases still routed through the vault: {offenders}"


def test_the_preamble_reaches_phase_5_without_the_vault_and_without_a_token():
    """§7 pointed at `[[phase-5-verify]]`, a vault wikilink, from the one file
    moved into the engine precisely to stop depending on one.

    A `{{WORKFLOW}}` token would fix that and break something else:
    `test_configurable_paths` records that a token-free preamble is SYMLINKED,
    which is why editing the rules every subagent runs on takes effect without
    an installer run. So §7 names the command instead — no vault, no token,
    both properties kept."""
    body = (ENGINE / "singularity" / "agents" / "AGENT_SHARED_PREAMBLE.md").read_text(
        encoding="utf-8")
    assert "[[phase-5-verify]]" not in body, "still routed through the vault"
    assert "{{" not in body, "a token here would stop the preamble being symlinked"
    assert "mb route --phase 5" in body, \
        "§7 must name a command that actually resolves phase 5 — plain `mb route` " \
        "returns the chain's ENTRY phase, so the instruction read as true and was not"


def test_the_workflow_token_is_substituted_not_shipped_literally():
    """Planted violation: `file_has_token` decides link-vs-render. A token it
    does not know about does not fail — the tree is LINKED and the literal
    `{{WORKFLOW}}` reaches the deployed file, where it resolves to nothing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vault_token_probe", ENGINE / "install" / "vault_token.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rendered = mod.render_text("см. {{WORKFLOW}}/phase-5-verify.md",
                               Path("/nonexistent-vault"))
    assert "{{WORKFLOW}}" not in rendered
    assert rendered.endswith("/singularity/workflow/phase-5-verify.md")

    probe = ENGINE / "singularity" / "skills" / "workflow-helper" / "SKILL.md"
    assert mod.file_has_token(probe), \
        "the workflow skill carries {{WORKFLOW}} but would be linked, not rendered"


# --- the CLI ------------------------------------------------------------------

def test_route_answers_without_a_vault():
    """Verified through the command a person types, not the function behind it:
    the chain lives in the engine, so `mb route` must answer on a machine whose
    vault is missing, empty or swapped."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "--vault", "/nonexistent-vault",
         "route", "почини падающий тест в индексаторе", "--json"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["class"] == "dev"
    assert payload["entry"] and payload["entry"].endswith("phase-SB-step-back.md")


def test_route_prints_nothing_actionable_for_chatter():
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "route", "спасибо", "--json"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["class"] is None


# --- the hook -----------------------------------------------------------------

def _run_hook(event: dict, vault: Path | None = None) -> dict:
    """Always against a throwaway vault: the hook writes a once-per-session
    marker, so a test sharing the real one passes the first time and then
    silently stops testing anything."""
    import os
    env = dict(os.environ)
    if vault is not None:
        env["MB_HOME"] = str(vault)
    proc = subprocess.run(
        [sys.executable, str(ENGINE / "singularity" / "hooks" / "user_prompt_submit.py")],
        input=json.dumps(event), capture_output=True, text=True, cwd=ENGINE, env=env)
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout or "{}")


def test_hook_announces_the_chain_for_a_task(tmp_path):
    (tmp_path / ".mb").mkdir()
    out = _run_hook({"hook_event_name": "UserPromptSubmit",
                     "session_id": "s1",
                     "prompt": "почини падающий тест в индексаторе"}, tmp_path)
    ctx = out.get("hookSpecificOutput", {}).get("additionalContext", "")
    assert "mb route" in ctx and "Step-Back" in ctx


def test_hook_announces_a_chain_once_per_session(tmp_path):
    """A chain restated on every prompt of one task is noise, and noise is how
    a gate teaches its reader to skip it. A DIFFERENT class still speaks."""
    (tmp_path / ".mb").mkdir()
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s2",
             "prompt": "почини падающий тест в индексаторе"}
    assert _run_hook(event, tmp_path) != {}, "first announcement never arrived"
    assert _run_hook(event, tmp_path) == {}, "the same chain announced twice"
    other = dict(event, prompt="сделай секцию лендинга с адаптивной вёрсткой")
    assert _run_hook(other, tmp_path) != {}, "a change of class must be announced"


def test_hook_is_silent_on_chatter(tmp_path):
    (tmp_path / ".mb").mkdir()
    assert _run_hook({"hook_event_name": "UserPromptSubmit",
                      "session_id": "s3", "prompt": "спасибо"}, tmp_path) == {}


def test_hook_never_blocks_and_never_decides():
    """`UserPromptSubmit` is a blockable event: exit 2 or `decision: block`
    ERASES the user's prompt. A router that can do that is a far worse failure
    than one that stays quiet."""
    src = (ENGINE / "singularity" / "hooks" / "user_prompt_submit.py").read_text(
        encoding="utf-8")
    assert '"decision"' not in src and "permissionDecision" not in src
    for junk in ('', 'not json at all', '{"prompt": 12345}', '{}'):
        proc = subprocess.run(
            [sys.executable, str(ENGINE / "singularity" / "hooks" / "user_prompt_submit.py")],
            input=junk, capture_output=True, text=True, cwd=ENGINE)
        assert proc.returncode == 0, f"{junk!r} made the hook exit {proc.returncode}"


def test_hook_output_fits_the_documented_cap():
    """UserPromptSubmit output strings are capped at 10 000 characters."""
    for key, chain in routing.CHAINS.items():
        text = routing.render(key, list(chain["markers"][:4]))
        assert len(text) < 10_000, f"{key}: injection is {len(text)} chars"


# --- registration -------------------------------------------------------------

def test_the_hook_is_registered_and_owned():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "register_hooks_probe", ENGINE / "install" / "register_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    entries = mod.HOOKS.get("UserPromptSubmit")
    assert entries, "the router is never installed"
    assert entries[0]["_mb"] is True, "an unowned entry cannot be cleanly uninstalled"
    assert "user_prompt_submit.py" in entries[0]["hooks"][0]["command"]
    assert "user_prompt_submit.py" in mod._OWNED_SCRIPTS["UserPromptSubmit"], \
        "ownership fallback missing — a pre-marker install would be orphaned"


# --- what the review of 2026-08-06 found ---------------------------------------
#
# Every test below was written BEFORE its fix and observed red. They guard the
# findings of a review that proved two earlier gates in this file were vacuous:
# both passed on the real form of the violation they claimed to catch.

def test_no_phase_file_links_to_a_sibling_as_a_wikilink():
    """The gate the first version of this file got wrong.

    `test_no_phase_file_addresses_the_vault_for_its_own_flow` searched for the
    literal `{{VAULT}}/workflow` — a string these files never contained. The
    real form is an Obsidian wikilink, `[[phase-3-implementation]]`, which
    resolves inside a vault and resolves to NOTHING here. 31 of them shipped.

    Links to vault KNOWLEDGE ([[Core Discipline - ...]]) are deliberately left
    alone: those are recall, and recall is allowed to be unavailable."""
    import re
    offenders = {}
    for p in sorted(routing.WORKFLOW.glob("*.md")):
        hits = re.findall(r"\[\[(phase-[^\]|]*|_router[^\]|]*)", p.read_text(encoding="utf-8"))
        if hits:
            offenders[p.name] = hits
    assert offenders == {}, (
        "phase files address each other as wikilinks, which resolve to nothing "
        f"outside a vault: {offenders}")


def test_a_skill_carrying_a_token_is_rendered_even_with_no_vault():
    """`{{WORKFLOW}}` resolves from `__file__` and needs no vault at all — but
    the link-vs-render gate is `vault is not None and tree_has_token(...)`, so
    with no vault the skill is SYMLINKED and the literal token ships. That is
    the exact machine state this whole change exists to serve."""
    import importlib.util
    import tempfile
    spec = importlib.util.spec_from_file_location(
        "register_skills_probe", ENGINE / "install" / "register_skills.py")
    rs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rs)

    dst = Path(tempfile.mkdtemp()) / "workflow-helper"
    rs._link_dir(rs.SKILLS_SRC / "workflow-helper", dst, None)
    deployed = (dst / "SKILL.md").read_text(encoding="utf-8")
    assert "{{WORKFLOW}}" not in deployed, \
        "a machine with no vault deploys a skill pointing at a literal token"


def test_route_resolves_any_phase_not_only_the_entry():
    """Preamble §7 tells every subagent that `mb route` gives the path to phase
    5. It gave the chain's ENTRY phase and nothing else, so the instruction was
    false — worse than the dead wikilink it replaced, because it looks right."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "route", "--phase", "5"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("phase-5-verify.md")
    assert Path(proc.stdout.strip()).is_file()


def test_the_router_survives_an_unwritable_marker_directory(tmp_path):
    """The docstring called the marker "a nicety, not a gate". It was a gate:
    `mkdir`/`touch` ran before `render`, so one OSError killed the feature
    permanently and silently — [[Recoverable-ошибка скрывает тотальный отказ]]."""
    (tmp_path / ".mb" / "markers").mkdir(parents=True)
    (tmp_path / ".mb" / "markers").chmod(0o500)
    try:
        out = _run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "ro",
                         "prompt": "почини падающий тест в индексаторе"}, tmp_path)
    finally:
        (tmp_path / ".mb" / "markers").chmod(0o700)
    assert out != {}, "an unwritable markers dir silently deleted the feature"


#: The class scanner the fix was owed. The first version shipped ONE hardcoded
#: instance (`assert "установ" not in ...`), which is a statement about one
#: marker, not about the class [[Rule - релиз против класса дефекта даёт сканер
#: класса]]. Each phrase names a real word that CONTAINS a marker but is not it.
NOT_INSTANCES = [
    ("зафиксируй ошибку в incidents", "dev"),
    ("добавь суффикс к именам файлов индекса", "dev"),
    ("поправь install/vault_token.py", "memory"),
    ("напиши документацию по аудитории пользователей", "review"),
    ("зарегистрируй хук в установщике проекта", "ops"),
]


@pytest.mark.parametrize("phrase,forbidden", NOT_INSTANCES)
def test_no_marker_fires_inside_another_word(phrase, forbidden):
    verdict = routing.classify(phrase)
    got = verdict[0] if verdict else None
    assert got != forbidden, \
        f"{phrase!r} routed to {forbidden} by {verdict[1] if verdict else '?'}"


def test_count_outranks_one_long_incidental_marker():
    """Length was the PRIMARY key, so a single long accidental match beat any
    number of short exact ones: a plain fix task went to «эксплуатация движка»."""
    key, markers = routing.classify("почини баг: mb doctor падает на пустом волте")
    assert key == "dev", f"routed to {key} by {markers}"


def test_the_router_ignores_system_notifications():
    """Background-task notifications arrive as prompts, so UserPromptSubmit
    fires on them. Observed live: a reviewer's report was classified as
    «эксплуатация движка» off the words `mb doctor` inside it."""
    notification = ("[SYSTEM NOTIFICATION - NOT USER INPUT]\n<task-notification>\n"
                    "Agent finished. It ran mb doctor and mb hooks status, and "
                    "checked the capture hooks configuration.\n</task-notification>")
    assert routing.classify(notification) is None


@pytest.mark.parametrize("prompt", ["запусти ревью", "сделай ревью", "почини баг"])
def test_a_short_but_real_task_is_not_silenced(prompt):
    """MIN_PROMPT_CHARS was set against "спасибо" and swallowed real commands:
    «запусти ревью» is 13 characters and carries the marker `ревью`."""
    assert routing.classify(prompt) is not None, f"{prompt!r} silenced by length"


def test_no_marker_can_be_unreachable():
    """Two shipped dead: `вёрстк` (the prompt is ё-folded, the markers are not)
    and `" css"` (a leading space cannot match a prompt that starts with it)."""
    for key, chain in routing.CHAINS.items():
        for m in chain["markers"]:
            assert "ё" not in m, f"{key}: {m!r} is ё-dead — the prompt folds ё to е"
            assert m == m.strip(), f"{key}: {m!r} pads with whitespace and is unreachable"


def test_session_id_is_sanitised_by_the_same_allow_list_as_module_context():
    """The hook used a DENY-list of 11 characters where the reference it names
    uses an ALLOW-list. Traversal was blocked either way, but control characters
    passed through, and two sanitisers for one job drift apart. One source."""
    from singularity.knowledge import module_context as mc
    import singularity.hooks.user_prompt_submit as ups

    for hostile in ("../../../../tmp/PWNED", "a\x00b", "a\x1bb\nc", "..", "/etc/passwd"):
        name = ups._marker(Path("/tmp/x"), hostile, "dev").name
        assert mc._SAFE_MARKER_RE.search(name.replace("route-claude-", "")
                                         .replace("-dev", "")) is None, \
            f"{hostile!r} survived sanitisation as {name!r}"
        assert ".." not in name and "/" not in name


def test_route_reports_a_vault_it_was_given_and_did_not_use():
    """`mb route` answers before the config load by design, so `--vault` is
    ignored — silently, where every other command fails loudly on a bad path.
    A typo then looks like it worked."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "--vault", "/nonexistent-vault",
         "route", "почини падающий тест"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "--vault" in proc.stderr, "an ignored flag must say so on stderr"
    assert "[mb route]" in proc.stdout, "the notice must not displace the answer"
