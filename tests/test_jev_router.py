"""The Jev router hook, run as the process the client runs.

The hard guarantee is negative: whatever goes wrong - flag off, wrong key, a
server that hangs, garbage on stdin - the hook exits 0 with nothing on stdout
inside the 5 s the client allows.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from jev_server import KEY, FakeJev, answers_for, reply

from singularity.config import Config
from singularity.jev import log, router

ENGINE = Path(__file__).resolve().parents[1]
HOOK = ENGINE / "singularity" / "hooks" / "jev_router.py"


def size_answer(score, conf):
    return {"type": "score", "score": score, "confidence": conf, "legend": {}, "probabilities": {}}


def verdict(score=2.0, conf=0.9, followup=0.1):
    return lambda req: answers_for(req["questions"], size=size_answer(score, conf),
                                   followup={"type": "noul", "noul": followup})


@pytest.fixture
def world(tmp_path):
    vault = tmp_path / "vault"
    (vault / ".mb").mkdir(parents=True)
    home = tmp_path / "home"
    home.mkdir()
    servers = []

    def make(*script, enabled=True, key=KEY):
        srv = FakeJev(script or [reply(200, verdict())])
        servers.append(srv)
        (home / ".singularity.json").write_text(json.dumps(
            {"vault_path": str(vault), **({"jev_enabled": True} if enabled else {})}))
        env = {**os.environ, "HOME": str(home), "MB_HOME": str(vault),
               "MB_JEV_ENDPOINT": srv.url, "MB_JEV_TEST": "1"}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["MB_JEV_API_KEY"] = key
        return srv, env

    make.vault, make.home = vault, home
    yield make
    for srv in servers:
        srv.close()


def run_hook(env, stdin, timeout=15):
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, str(HOOK)], input=stdin, capture_output=True,
                          text=True, env=env, cwd=ENGINE, timeout=timeout)
    return proc, time.monotonic() - t0


def event(prompt="refactor the indexer across the modules", session="s1"):
    return json.dumps({"prompt": prompt, "session_id": session})


def journal(world):
    cfg = Config(vault=world.vault, data={})
    return log.read(cfg)


def test_flag_off_is_silent_and_touches_nothing(world):
    srv, env = world(enabled=False)
    proc, _ = run_hook(env, event())
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert not srv.requests and journal(world) == []


def test_success_injects_a_hint_and_journals_it(world):
    srv, env = world(reply(200, verdict(score=2.2, conf=0.85, followup=0.1),
                           {"x-typesafe-request-id": "req_1"}))
    proc, _ = run_hook(env, event())
    assert proc.returncode == 0 and proc.stderr == ""
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "UserPromptSubmit"
    assert "size=medium" in out["additionalContext"] and "sonnet·medium" in out["additionalContext"]
    [row] = journal(world)
    assert row["kind"] == "router" and row["decided"] == "jev" and row["route"] == "sonnet·medium"
    assert row["size"] == "medium" and row["test"] is True and row["request_id"] == "req_1"
    assert row["cost"] > 0 and row["error"] is None
    assert KEY not in json.dumps(row)


def test_low_confidence_is_silent_and_decided_self(world):
    _, env = world(reply(200, verdict(conf=0.4)))
    proc, _ = run_hook(env, event())
    assert proc.returncode == 0 and proc.stdout == ""
    [row] = journal(world)
    assert row["decided"] == "self" and row["route"] is None


def test_a_bad_key_is_silent_fast_and_journalled(world):
    srv, env = world(reply(401, {"detail": {"error_type": "authentication_error"}}),
                     key="wrong-key-value")
    proc, took = run_hook(env, event())
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert took < 5
    [row] = journal(world)
    assert row["error"] == "http:401" and row["cost"] == 0
    assert "wrong-key-value" not in json.dumps(row)


def test_a_server_that_sleeps_cannot_hold_the_hook_past_5s(world):
    _, env = world(reply(200, verdict(), delay=10))
    proc, took = run_hook(env, event())
    assert proc.returncode == 0 and proc.stdout == ""
    assert took < 5
    [row] = journal(world)
    assert row["error"] == "timeout:null"


def test_the_watchdog_exits_zero_on_its_own():
    """Independent of Jev's own timeout: whatever hangs, the timer ends it."""
    code = (
        f"import sys; sys.path.insert(0, {str(ENGINE)!r})\n"
        "from singularity.hooks import jev_router as h\n"
        "h.WATCHDOG_SECONDS = 0.3\n"
        "h._arm_watchdog()\n"
        "import time; time.sleep(10)\n")
    t0 = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert proc.returncode == 0 and time.monotonic() - t0 < 5


@pytest.mark.parametrize("junk", ["", "not json", "[1, 2]", '{"prompt": 12345}', '{"prompt": null}',
                                  "\xff\xfe garbage"])
def test_garbage_stdin_exits_zero_silently(world, junk):
    srv, env = world()
    proc, _ = run_hook(env, junk)
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    assert not srv.requests


@pytest.mark.parametrize("prompt", ["", "   ", "/jev status", "  /clear"])
def test_empty_and_slash_prompts_are_not_sent(world, prompt):
    srv, env = world()
    proc, _ = run_hook(env, event(prompt))
    assert proc.returncode == 0 and proc.stdout == ""
    assert not srv.requests and journal(world) == []


def test_a_long_prompt_is_truncated_for_the_state(world):
    srv, env = world()
    run_hook(env, event("x" * 25_000))
    assert len(srv.requests[0]["body"]["state"]) == 6000


def test_the_state_is_redacted_before_it_leaves(world):
    srv, env = world()
    secret = "sk-ant-api03-" + "A" * 40
    run_hook(env, event(f"deploy with {secret} now"))
    assert secret not in srv.sent_text()


def test_a_followup_reuses_the_sessions_previous_route(world):
    _, env = world(reply(200, verdict(score=3.0, conf=0.9, followup=0.05)),
                   reply(200, verdict(score=0.2, conf=0.9, followup=0.9)))
    first, _ = run_hook(env, event("redesign the whole indexer", "sess"))
    assert "opus·high" in first.stdout
    second, _ = run_hook(env, event("yes, but shorter", "sess"))
    assert "opus·high" in json.loads(second.stdout)["hookSpecificOutput"]["additionalContext"]
    assert [r["route"] for r in journal(world)] == [router.ROUTES["large"]] * 2


def test_a_followup_without_history_is_left_to_the_lead(world):
    """ASSUMED (docs/jev.md): what a reply continues is unknown without a
    previous route, and "yes, do the whole plan" reads as a tiny task."""
    _, env = world(reply(200, verdict(score=0.2, conf=0.9, followup=0.9)))
    proc, _ = run_hook(env, event("yes", "fresh"))
    assert proc.stdout == ""
    [row] = journal(world)
    assert row["decided"] == "self" and row["route"] is None


def test_a_followup_is_judged_before_the_size_confidence(world):
    """Where the size of a short reply is least certain is where the follow-up
    path has to work."""
    _, env = world(reply(200, verdict(score=3.0, conf=0.9, followup=0.05)),
                   reply(200, verdict(score=0.1, conf=0.45, followup=0.95)))
    run_hook(env, event("redesign the whole indexer", "sess"))
    proc, _ = run_hook(env, event("yes, go ahead", "sess"))
    assert "sonnet·high executors" in proc.stdout
    assert journal(world)[1]["decided"] == "jev"


@pytest.mark.parametrize("prompt", [
    "<task-notification>\n<task-id>x</task-id></task-notification>",
    "<system-reminder>\nthe date changed\n</system-reminder>",
    "[SYSTEM NOTIFICATION - NOT USER INPUT] background task finished",
    "<local-command-caveat>Caveat: ...</local-command-caveat>",
])
def test_harness_turns_are_neither_sent_nor_journalled(world, prompt):
    srv, env = world()
    proc, _ = run_hook(env, event(prompt))
    assert proc.returncode == 0 and proc.stdout == ""
    assert not srv.requests and journal(world) == []


def test_a_path_is_a_prompt_but_a_slash_command_is_not(world):
    srv, env = world()
    run_hook(env, event("/Users/me/project/x.py fails on import, fix it"))
    assert len(srv.requests) == 1
    assert router.skip_reason("/jev status") == "slash command"
    assert router.skip_reason("/mb:search term") == "slash command"
    assert router.skip_reason("/clear") == "slash command"
    assert router.skip_reason("please quote <task-notification> in the docs") is None


def test_a_non_finite_answer_is_a_journal_error(world):
    _, env = world(reply(200, '{"model":"m","answers":{"size":{"score":NaN,"confidence":NaN},'
                              '"followup":{"noul":0.1}}}'))
    proc, _ = run_hook(env, event())
    assert proc.stdout == ""
    assert journal(world)[0]["error"] == "bad_json:null"


def test_the_midpoints_round_up_symmetrically():
    for score, size in ((0.5, "small"), (1.5, "medium"), (2.5, "large")):
        out = router.decide({"size": size_answer(score, 0.9), "followup": {"noul": 0.0}})
        assert out["size"] == size


def test_whatever_ask_raises_the_router_journals_and_stays_silent(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    (vault / ".mb").mkdir(parents=True)
    cfg = Config(vault=vault, data={"jev_enabled": True})
    monkeypatch.setattr(router.client, "resolve_key", lambda: "k")

    def boom(*a, **k):
        raise RuntimeError("anything")

    monkeypatch.setattr(router.client, "ask", boom)
    assert router.route(cfg, "do the thing", "s") is None
    assert log.read(cfg)[0]["error"] == "internal:null"


def test_enabled_without_a_key_journals_and_stays_silent(tmp_path, monkeypatch):
    vault = tmp_path / "v"
    (vault / ".mb").mkdir(parents=True)
    cfg = Config(vault=vault, data={"jev_enabled": True})
    monkeypatch.setattr(router.client, "resolve_key", lambda: None)
    assert router.route(cfg, "do the thing", "s") is None
    assert log.read(cfg)[0]["error"] == "no_key:null"


def test_a_malformed_answer_is_a_journal_error_not_a_crash(world):
    _, env = world(reply(200, lambda req: answers_for(req["questions"], size={"type": "score"})))
    proc, _ = run_hook(env, event())
    assert proc.returncode == 0 and proc.stdout == "" and proc.stderr == ""
    [row] = journal(world)
    assert row["error"] == "bad_json:null" and row["decided"] is None


@pytest.mark.parametrize("score,size", [(-3, "tiny"), (0.4, "tiny"), (1.5, "medium"),
                                        (2.4, "medium"), (9, "large")])
def test_decide_clamps_the_score_to_the_four_levels(score, size):
    out = router.decide({"size": size_answer(score, 0.9), "followup": {"noul": 0.0}})
    assert out["size"] == size and out["route"] == router.ROUTES[size]


def test_the_hook_source_cannot_stop_a_prompt():
    """Scans the hook and the router module for the two ways to erase a prompt:
    exit code 2 and a blocking decision."""
    for path in (HOOK, ENGINE / "singularity" / "jev" / "router.py"):
        src = path.read_text(encoding="utf-8")
        assert "exit(2)" not in src, path.name
        assert '"block"' not in src and "'block'" not in src, path.name
        assert '"decision"' not in src, path.name


def test_the_hook_is_registered_second_owned_and_given_five_seconds():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "register_hooks_jev_probe", ENGINE / "install" / "register_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    entries = mod.HOOKS["UserPromptSubmit"]
    assert "user_prompt_submit.py" in entries[0]["hooks"][0]["command"]
    assert "jev_router.py" in entries[1]["hooks"][0]["command"]
    assert entries[1]["_mb"] is True and entries[1]["hooks"][0]["timeout"] == 5
    assert "jev_router.py" in mod._OWNED_SCRIPTS["UserPromptSubmit"]


def _registered_command():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "register_hooks_guard_probe", ENGINE / "install" / "register_hooks.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.HOOKS["UserPromptSubmit"][1]["hooks"][0]["command"]


@pytest.mark.parametrize("body", [None, "import sys\nsys.exit(2)\n",
                                  "raise RuntimeError('broken')\n", "syntax error here\n"])
def test_the_registered_command_exits_zero_whatever_happens_to_the_hook(tmp_path, body):
    """A bare `python missing.py` exits 2, which erases the prompt. Runs the exact
    registered command string against a copy of the hook that is missing, exits
    2, raises or does not even parse."""
    cmd = _registered_command()
    real = (ENGINE / "singularity" / "hooks" / "jev_router.py").as_posix()
    assert real in cmd
    target = tmp_path / "jev_router.py"
    if body is not None:
        target.write_text(body)
    proc = subprocess.run(cmd.replace(real, target.as_posix()), shell=True,
                          input=event(), capture_output=True, text=True, timeout=20)
    assert proc.returncode == 0 and proc.stdout == ""


def test_the_registered_command_still_delivers_the_hooks_answer(world):
    srv, env = world(reply(200, verdict(score=1.0, conf=0.9, followup=0.1)))
    proc = subprocess.run(_registered_command(), shell=True, input=event(), capture_output=True,
                          text=True, env=env, cwd=ENGINE, timeout=20)
    assert proc.returncode == 0
    assert "haiku·low" in json.loads(proc.stdout)["hookSpecificOutput"]["additionalContext"]
