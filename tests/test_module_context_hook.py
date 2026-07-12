"""T07: Claude PreToolUse adapter (singularity/hooks/module_context.py).

Covers the full chain: tool filter -> file_path extraction -> project
membership by file location -> module match -> once-per-session marker ->
budgeted note excerpt -> additionalContext-only output (never
permissionDecision, never a denial).
"""
import io
import json

import pytest

from singularity.hooks import module_context as mc


@pytest.fixture
def vault(tmp_path):
    return tmp_path


def _resolve_to(monkeypatch, value):
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: value)


def _setup_project(vault, project_id="p", project_dirname="proj"):
    proj_dir = vault / "code" / project_dirname
    proj_dir.mkdir(parents=True)
    mb = vault / ".mb"; mb.mkdir(parents=True, exist_ok=True)
    (mb / "config.json").write_text(json.dumps(
        {"projects": {project_id: {"path": str(proj_dir)}}}), encoding="utf-8")
    return proj_dir


def _set_module_map(vault, project_id, module_map):
    (vault / ".mb" / "context.json").write_text(
        json.dumps({project_id: {"context_map": module_map}}), encoding="utf-8")


def _write_note(vault, rel, text):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --- build_additional_context: no-op cases --------------------------------------

def test_non_write_tool_is_noop(monkeypatch, vault):
    _resolve_to(monkeypatch, vault)
    assert mc.build_additional_context({"tool_name": "Read", "tool_input": {"file_path": "x"}}) is None


def test_missing_file_path_is_noop(monkeypatch, vault):
    _resolve_to(monkeypatch, vault)
    assert mc.build_additional_context({"tool_name": "Edit", "tool_input": {}}) is None


def test_malformed_event_is_noop(monkeypatch, vault):
    _resolve_to(monkeypatch, vault)
    assert mc.build_additional_context("not a dict") is None
    assert mc.build_additional_context({}) is None


def test_no_vault_is_noop(monkeypatch):
    _resolve_to(monkeypatch, None)
    ctx = mc.build_additional_context({"tool_name": "Edit", "tool_input": {"file_path": "x"}})
    assert ctx is None


def test_file_outside_any_project_is_noop(monkeypatch, vault):
    _setup_project(vault)
    _resolve_to(monkeypatch, vault)
    ctx = mc.build_additional_context({
        "tool_name": "Edit", "cwd": str(vault),
        "tool_input": {"file_path": str(vault / "elsewhere" / "x.py")}})
    assert ctx is None


def test_project_found_but_no_module_match_is_noop(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _resolve_to(monkeypatch, vault)
    ctx = mc.build_additional_context({
        "tool_name": "Edit", "cwd": str(proj),
        "tool_input": {"file_path": str(proj / "unrelated" / "x.py")}})
    assert ctx is None


# --- build_additional_context: real match, once-per-session ----------------------

def test_full_match_injects_and_marks(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook architecture facts.")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
            "tool_input": {"file_path": str(proj / "hooks" / "stop_capture.py")}}
    ctx = mc.build_additional_context(event)
    assert ctx is not None
    assert "Hook architecture facts." in ctx
    assert "Module context (p/hooks)" in ctx


def test_second_call_same_module_same_session_is_silent(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
            "tool_input": {"file_path": str(proj / "hooks" / "a.py")}}
    assert mc.build_additional_context(event) is not None
    event2 = dict(event, tool_input={"file_path": str(proj / "hooks" / "b.py")})   # same module
    assert mc.build_additional_context(event2) is None


def test_second_module_in_same_session_gets_its_own_context(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {
        "hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"},
        "engine": {"globs": ["engine"], "note": "reference/Engine.md"},
    })
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _write_note(vault, "reference/Engine.md", "Engine facts.")
    _resolve_to(monkeypatch, vault)
    base = {"tool_name": "Edit", "cwd": str(proj), "session_id": "s1"}
    ctx1 = mc.build_additional_context({**base, "tool_input": {"file_path": str(proj / "hooks" / "a.py")}})
    ctx2 = mc.build_additional_context({**base, "tool_input": {"file_path": str(proj / "engine" / "b.py")}})
    assert ctx1 is not None and "Hook facts." in ctx1
    assert ctx2 is not None and "Engine facts." in ctx2


def test_different_session_gets_fresh_injection(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _resolve_to(monkeypatch, vault)
    fp = str(proj / "hooks" / "a.py")
    ctx1 = mc.build_additional_context({"tool_name": "Edit", "cwd": str(proj),
                                        "session_id": "s1", "tool_input": {"file_path": fp}})
    ctx2 = mc.build_additional_context({"tool_name": "Edit", "cwd": str(proj),
                                        "session_id": "s2", "tool_input": {"file_path": fp}})
    assert ctx1 is not None and ctx2 is not None


# --- MultiEdit + relative path resolution -----------------------------------------

def test_multiedit_tool_is_recognized(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "MultiEdit", "cwd": str(proj), "session_id": "s1",
             "tool_input": {"file_path": str(proj / "hooks" / "a.py")}}
    assert mc.build_additional_context(event) is not None


def test_relative_file_path_resolved_against_event_cwd(monkeypatch, vault):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
             "tool_input": {"file_path": "hooks/a.py"}}     # relative, not absolute
    assert mc.build_additional_context(event) is not None


# --- symlink escape / containment ------------------------------------------------

def test_symlink_escaping_project_root_is_rejected(monkeypatch, vault, tmp_path):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    outside = tmp_path.parent / "outside-real-dir"
    outside.mkdir(exist_ok=True)
    (outside / "evil.py").write_text("x", encoding="utf-8")
    link = proj / "hooks"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation not permitted on this machine")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
             "tool_input": {"file_path": str(proj / "hooks" / "evil.py")}}
    # End-to-end regression test for the whole hook pipeline. The actual
    # defense is projects.detect_project's own resolve()-based containment
    # (T03, already unit-tested there) — abs_file follows the symlink OUT of
    # the project root, so detect_project returns no project before this
    # hook's own relative_to() containment check is even reached. Confirmed
    # by temporarily no-op'ing relative_to(): this test still passed,
    # proving detect_project is the layer actually doing the rejection here.
    assert mc.build_additional_context(event) is None


# --- fail-open on malformed config/context.json -----------------------------------

def test_malformed_config_json_is_noop_not_a_crash(monkeypatch, vault):
    mb = vault / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(vault), "tool_input": {"file_path": "x.py"}}
    assert mc.build_additional_context(event) is None


def test_malformed_context_json_is_noop_not_a_crash(monkeypatch, vault):
    proj = _setup_project(vault)
    (vault / ".mb" / "context.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(proj),
             "tool_input": {"file_path": str(proj / "x.py")}}
    assert mc.build_additional_context(event) is None    # must not raise


# --- main(): wire contract ---------------------------------------------------------

def _run(monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(mc.sys.stdin, "buffer",
                        io.BytesIO(stdin_text.encode("utf-8")), raising=False)
    rc = mc.main()
    return rc, json.loads(capsys.readouterr().out.strip())


def test_main_noop_emits_empty_object(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    rc, payload = _run(monkeypatch, capsys, json.dumps({"tool_name": "Read"}))
    assert rc == 0
    assert payload == {}


def test_main_never_emits_permission_decision(monkeypatch, vault, capsys):
    proj = _setup_project(vault)
    _set_module_map(vault, "p", {"hooks": {"globs": ["hooks"], "note": "reference/Hooks.md"}})
    _write_note(vault, "reference/Hooks.md", "Hook facts.")
    _resolve_to(monkeypatch, vault)
    event = json.dumps({"tool_name": "Edit", "cwd": str(proj), "session_id": "s1",
                        "tool_input": {"file_path": str(proj / "hooks" / "a.py")}})
    rc, payload = _run(monkeypatch, capsys, event)
    assert rc == 0
    assert "permissionDecision" not in payload.get("hookSpecificOutput", {})
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "Hook facts." in payload["hookSpecificOutput"]["additionalContext"]


def test_main_malformed_stdin_never_crashes(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    rc, payload = _run(monkeypatch, capsys, "not json {{{")
    assert rc == 0
    assert payload == {}


def test_main_builder_exception_is_isolated(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    monkeypatch.setattr(mc, "build_additional_context",
                        lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    rc, payload = _run(monkeypatch, capsys, json.dumps({"tool_name": "Edit"}))
    assert rc == 0
    assert payload == {}
