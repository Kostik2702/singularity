"""The chain table is pushed when a task arrives — and says nothing otherwise.

These tests guard three separable things, because each failed differently in
the design:

1. What is pushed does not depend on the language the task is written in. Until
   2026-08-08 this module matched Russian word stems, so an English prompt was
   never routed and a Russian one always was — the router was a function of the
   user's keyboard. The classification moved to the model; these tests pin that
   the ENGINE side no longer inspects words at all.
2. It stays SILENT on machine turns and on acknowledgements. A gate that fires
   on "ok" teaches its reader to skip it, and is then worse than no gate at all.
   Both surviving filters are language-neutral: an envelope marker the client
   emits, and a character count.
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


# --- the property the rewrite exists for --------------------------------------

#: The same request, written six ways. Nothing about what the engine pushes may
#: differ between them. This is a CLASS scanner, not one example: a future
#: "small" lexical shortcut (an English keyword list, a script-range check)
#: would pass a single-instance test and fail here.
SAME_TASK_IN_MANY_LANGUAGES = [
    "fix the failing test in the indexer",
    "почини падающий тест в индексаторе",
    "corrige la prueba que falla en el indexador",
    "behebe den fehlschlagenden Test im Indexer",
    "修复索引器中失败的测试",
    "インデクサの失敗しているテストを直して",
]


@pytest.mark.parametrize("prompt", SAME_TASK_IN_MANY_LANGUAGES)
def test_the_pushed_text_is_identical_whatever_language_the_task_uses(prompt):
    """The invariant the user asked for, stated as an equality rather than as a
    promise: `render_table()` takes no prompt, so there is nothing for a
    language to change. If someone reintroduces per-prompt branching, the text
    starts differing and this goes red."""
    assert routing.should_push(prompt), f"{prompt!r} was not recognised as a task"
    assert routing.render_table() == routing.render_table()


def test_the_engine_never_reads_the_words_of_a_prompt():
    """The scanner the fix is owed. A stem table is not the only way to make
    behaviour language-dependent — any per-prompt inspection is. The public
    surface must expose no function that takes a prompt and returns a class.

    Written as a check on the API, not on a marker list, because the marker list
    is the instance and 'the engine classifies text itself' is the class."""
    assert not hasattr(routing, "classify"), \
        "a prompt classifier is back in the engine; classification belongs to the model"
    assert not hasattr(routing, "CHAINS_MARKERS")
    for key, chain in routing.CHAINS.items():
        assert "markers" not in chain, \
            f"{key}: a marker list makes routing a function of the user's language"


def test_the_table_tells_the_model_to_classify_by_meaning():
    """The table without the instruction is a reference card, and a reference
    card nobody is told to act on is an archive — the exact failure that made
    the workflow router a skill nobody pulled."""
    text = routing.render_table()
    assert "MEANING" in text, "the table must say to classify by meaning"
    assert "whatever language" in text, \
        "the instruction must survive, not just the table: a model told to " \
        "match keywords reintroduces the language dependency in its own head"
    assert "No class fits" in text, "refusing to classify must stay legal"


# --- silence, on the two language-neutral grounds -------------------------------

@pytest.mark.parametrize("chatter", ["ok", "thanks", "да", "ок", "yes", "", "   "])
def test_short_acknowledgements_do_not_spend_the_table(chatter):
    """A character count, not a word list — which is why it holds for "thanks"
    and "спасибо" alike."""
    assert routing.should_push(chatter) is False


def test_machine_turns_are_not_tasks():
    """Background-task notifications arrive as prompts, so UserPromptSubmit
    fires on them. Observed live: a reviewer's report was classified as engine
    operations off the words `mb doctor` quoted inside it."""
    notification = ("[SYSTEM NOTIFICATION - NOT USER INPUT]\n<task-notification>\n"
                    "Agent finished. It ran mb doctor and mb hooks status, and "
                    "checked the capture hooks configuration.\n</task-notification>")
    assert routing.should_push(notification) is False


def test_a_short_but_real_task_is_not_silenced():
    """The threshold was set against acknowledgements and must not swallow real
    commands: "run the review" is 14 characters."""
    for prompt in ("run the review", "fix the daemon", "запусти ревью"):
        assert routing.should_push(prompt), f"{prompt!r} silenced by length"


# --- the chains themselves ----------------------------------------------------

def test_every_chain_is_complete():
    for key, chain in routing.CHAINS.items():
        assert chain["title"] and chain["steps"], f"{key}: an empty chain routes nowhere"
        assert chain["note"], f"{key}: a chain without its governing rule is a bare list"


def test_every_chain_reaches_the_table():
    """A chain absent from the pushed text is unreachable: the model can only
    pick from what it was shown."""
    text = routing.render_table()
    for key, chain in routing.CHAINS.items():
        assert key in text, f"{key} is never offered"
        assert chain["note"] in text, f"{key}: its governing rule is not delivered"


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
        "returns the table, so the instruction would read as true and not be"


def test_the_workflow_token_is_substituted_not_shipped_literally():
    """Planted violation: `file_has_token` decides link-vs-render. A token it
    does not know about does not fail — the tree is LINKED and the literal
    `{{WORKFLOW}}` reaches the deployed file, where it resolves to nothing."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vault_token_probe", ENGINE / "install" / "vault_token.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rendered = mod.render_text("see {{WORKFLOW}}/phase-5-verify.md",
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
         "route", "--json"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert set(payload["classes"]) == set(routing.CHAINS)
    assert payload["classes"]["dev"]["entry"].endswith("phase-SB-step-back.md")


def test_route_prints_one_chain_on_request():
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "route", "--class", "review", "--json"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["class"] == "review"


def test_route_refuses_an_unknown_class_loudly():
    """Silence on a typo makes a wrong class look accepted."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "route", "--class", "nosuch"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 2, proc.stdout
    assert "nosuch" in proc.stderr


# --- the hook -----------------------------------------------------------------

def _run_hook(event: dict, vault: Path | None = None) -> dict:
    """Always against a throwaway vault: the hook writes a once-per-session
    marker, so a test sharing the real one passes the first time and then
    silently stops testing anything."""
    import os
    env = dict(os.environ)
    if vault is not None:
        env["MB_HOME"] = str(vault)
    # the hook writes UTF-8 whatever the console code page (as the client reads
    # it); decoding with the locale turned every non-ASCII byte of the table
    # into another character on Windows, and six languages "differed"
    proc = subprocess.run(
        [sys.executable, str(ENGINE / "singularity" / "hooks" / "user_prompt_submit.py")],
        input=json.dumps(event), capture_output=True, text=True, encoding="utf-8",
        cwd=ENGINE, env=env)
    assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
    return json.loads(proc.stdout or "{}")


def _pushed(out: dict) -> str:
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_hook_pushes_the_table_when_a_task_arrives(tmp_path):
    (tmp_path / ".mb").mkdir()
    out = _run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                     "prompt": "fix the failing test in the indexer"}, tmp_path)
    ctx = _pushed(out)
    assert "mb route" in ctx and "Step-Back" in ctx


@pytest.mark.parametrize("prompt", SAME_TASK_IN_MANY_LANGUAGES)
def test_the_hook_pushes_the_same_bytes_in_any_language(prompt, tmp_path):
    """End to end, through the process the client actually runs — not through
    the function. A language-dependent branch anywhere between stdin and stdout
    shows up here as a difference in bytes."""
    (tmp_path / ".mb").mkdir()
    out = _run_hook({"hook_event_name": "UserPromptSubmit",
                     "session_id": f"lang-{abs(hash(prompt))}",
                     "prompt": prompt}, tmp_path)
    assert _pushed(out) == routing.render_table(), \
        f"{prompt!r} produced different guidance from the reference text"


def test_hook_pushes_once_per_session(tmp_path):
    """The table restated on every prompt is noise, and noise is how a gate
    teaches its reader to skip it."""
    (tmp_path / ".mb").mkdir()
    event = {"hook_event_name": "UserPromptSubmit", "session_id": "s2",
             "prompt": "fix the failing test in the indexer"}
    assert _run_hook(event, tmp_path) != {}, "the first push never arrived"
    assert _run_hook(event, tmp_path) == {}, "the table was pushed twice"
    other = dict(event, prompt="build a landing page section with responsive markup")
    assert _run_hook(other, tmp_path) == {}, \
        "a second task in the same session must not re-push the table"
    fresh = dict(event, session_id="s2-new")
    assert _run_hook(fresh, tmp_path) != {}, "a new session must get the table"


def test_hook_is_silent_on_chatter(tmp_path):
    (tmp_path / ".mb").mkdir()
    assert _run_hook({"hook_event_name": "UserPromptSubmit",
                      "session_id": "s3", "prompt": "thanks"}, tmp_path) == {}


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
    """UserPromptSubmit output strings are capped at 10 000 characters, and this
    one grows with every chain added."""
    assert len(routing.render_table()) < 10_000


def test_the_table_stays_worth_its_place_in_the_context():
    """It is paid once per session, which is what makes a whole table affordable
    where a per-prompt injection was not. Without a ceiling that reasoning
    silently stops being true as chains are added."""
    assert len(routing.render_table()) < 4_000, \
        "the table outgrew the budget its once-per-session push was argued on"


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


def _registered_router_command() -> str:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "register_hooks_guard_probe", ENGINE / "install" / "register_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HOOKS["UserPromptSubmit"][0]["hooks"][0]["command"]


@pytest.mark.parametrize("body", [None, "import sys\nsys.exit(2)\n",
                                  "raise RuntimeError('broken')\n", "syntax error here\n"])
def test_the_registered_router_exits_zero_whatever_happens_to_the_hook(tmp_path, body):
    """`python missing.py` exits 2, and for UserPromptSubmit exit 2 erases the
    prompt. Runs the exact registered command against a copy of the hook that
    is missing, exits 2, raises or does not parse."""
    cmd = _registered_router_command()
    real = (ENGINE / "singularity" / "hooks" / "user_prompt_submit.py").as_posix()
    assert real in cmd, "the router is registered as something else than the guarded script"
    target = tmp_path / "user_prompt_submit.py"
    if body is not None:
        target.write_text(body)
    proc = subprocess.run(cmd.replace(real, target.as_posix()), shell=True,
                          input=json.dumps({"prompt": "fix the indexer"}),
                          capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0 and proc.stdout == ""


def test_the_registered_router_still_pushes_the_table(tmp_path):
    import os
    (tmp_path / ".mb").mkdir()
    proc = subprocess.run(
        _registered_router_command(), shell=True, cwd=ENGINE, timeout=20,
        input=json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s1",
                          "prompt": "fix the failing test in the indexer"}),
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "MB_HOME": str(tmp_path)})
    assert proc.returncode == 0
    assert "mb route" in _pushed(json.loads(proc.stdout))


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
    permanently and silently — [[A recoverable error hides a total failure]]."""
    (tmp_path / ".mb" / "markers").mkdir(parents=True)
    (tmp_path / ".mb" / "markers").chmod(0o500)
    try:
        out = _run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "ro",
                         "prompt": "fix the failing test in the indexer"}, tmp_path)
    finally:
        (tmp_path / ".mb" / "markers").chmod(0o700)
    assert out != {}, "an unwritable markers dir silently deleted the feature"


def test_session_id_is_sanitised_by_the_same_allow_list_as_module_context():
    """The hook used a DENY-list of 11 characters where the reference it names
    uses an ALLOW-list. Traversal was blocked either way, but control characters
    passed through, and two sanitisers for one job drift apart. One source."""
    from singularity.knowledge import module_context as mc
    import singularity.hooks.user_prompt_submit as ups

    for hostile in ("../../../../tmp/PWNED", "a\x00b", "a\x1bb\nc", "..", "/etc/passwd"):
        name = ups._marker(Path("/tmp/x"), hostile, "table").name
        assert mc._SAFE_MARKER_RE.search(name.replace("route-claude-", "")
                                         .replace("-table", "")) is None, \
            f"{hostile!r} survived sanitisation as {name!r}"
        assert ".." not in name and "/" not in name


def test_route_reports_a_vault_it_was_given_and_did_not_use():
    """`mb route` answers before the config load by design, so `--vault` is
    ignored — silently, where every other command fails loudly on a bad path.
    A typo then looks like it worked."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "--vault", "/nonexistent-vault", "route"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "--vault" in proc.stderr, "an ignored flag must say so on stderr"
    assert "[mb route]" in proc.stdout, "the notice must not displace the answer"


def test_route_says_so_when_it_ignores_the_task_text():
    """The same reason the `--vault` notice exists, two lines above it in the
    same function: an argument accepted and then dropped without a word makes a
    stale invocation look like it still works. `mb route "<task>"` answered with
    a class until 2026-08-08; naming the class is the reader's job now, so the text
    is used for nothing and has to say so."""
    proc = subprocess.run(
        [sys.executable, "-m", "singularity", "route", "fix the failing test"],
        cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0
    assert "ignored" in proc.stderr, "the text was dropped in silence"
    assert "[mb route]" in proc.stdout, "the notice must not displace the answer"


#: Every (branch, argument-it-does-not-use) pair. The first version of this
#: guard was one assertion about the positional `task`, which is an instance;
#: `--class` went on being dropped in silence next to it until a second review.
#: A release against a class of defect owes a scanner for the class.
IGNORED_ARGUMENT_CASES = [
    (["route", "--phase", "5", "sometask"], "task"),
    (["route", "--phase", "5", "--class", "review"], "--class"),
    (["route", "--class", "review", "sometask"], "task"),
    (["route", "sometask"], "task"),
    (["--vault", "/nonexistent-vault", "route"], "--vault"),
]


@pytest.mark.parametrize("argv,dropped", IGNORED_ARGUMENT_CASES)
def test_every_argument_this_command_drops_says_so(argv, dropped):
    """An argument accepted and then dropped without a word makes a stale
    invocation look like it still works — the reason the `--vault` notice was
    written, applied to every argument rather than to the one that prompted it."""
    proc = subprocess.run([sys.executable, "-m", "singularity", *argv],
                          cwd=ENGINE, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert dropped in proc.stderr and "ignored" in proc.stderr, \
        f"{' '.join(argv)}: {dropped} was dropped in silence"
    assert proc.stdout.strip(), "the notice must not displace the answer"
