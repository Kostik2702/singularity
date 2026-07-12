"""T08: Claude PreToolUse vault-guard adapter (singularity/hooks/vault_guard.py).

Covers: opt-in disabled-by-default, audit mode (additionalContext only,
NEVER permissionDecision), deny mode (explicit opt-in), existing-note edits
always allowed, templates/compaction-plans carve-outs, config.json direct-
edit detection, and the main() wire contract.
"""
import io
import json

import pytest

from singularity.hooks import vault_guard as vg


@pytest.fixture
def vault(tmp_path):
    return tmp_path


def _resolve_to(monkeypatch, value):
    import singularity.config as config_mod
    monkeypatch.setattr(config_mod, "resolve_vault", lambda explicit=None: value)


def _write_cfg(vault, **kw):
    mb = vault / ".mb"; mb.mkdir(parents=True, exist_ok=True)
    (mb / "config.json").write_text(json.dumps(kw), encoding="utf-8")


# --- disabled by default -----------------------------------------------------------

def test_disabled_by_default_even_on_new_managed_file(monkeypatch, vault):
    _write_cfg(vault)     # no vault_guard_enabled key at all
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "howto" / "new.md")}}
    assert vg.evaluate(event) is None


def test_explicitly_disabled(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=False)
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "howto" / "new.md")}}
    assert vg.evaluate(event) is None


# --- audit mode (default once enabled) ----------------------------------------------

def test_audit_mode_flags_new_managed_file_context_only(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True)
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "howto" / "new.md")}}
    out = vg.evaluate(event)
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "additionalContext" in hso and "howto" in hso["additionalContext"]


def test_audit_mode_flags_config_json_edit(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True)
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / ".mb" / "config.json")}}
    out = vg.evaluate(event)
    assert out is not None
    assert "permissionDecision" not in out["hookSpecificOutput"]
    assert "config.json" in out["hookSpecificOutput"]["additionalContext"]


def test_default_mode_is_audit_when_unspecified(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True)      # no vault_guard_mode key
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "decisions" / "new.md")}}
    out = vg.evaluate(event)
    assert "permissionDecision" not in out["hookSpecificOutput"]


# --- deny mode (explicit opt-in only) ------------------------------------------------

def test_deny_mode_blocks_with_reason(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True, vault_guard_mode="deny")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "rules" / "new.md")}}
    out = vg.evaluate(event)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "permissionDecisionReason" in hso
    assert "additionalContext" not in hso


# --- always-allowed cases -------------------------------------------------------------

def test_editing_existing_note_never_flagged(monkeypatch, vault):
    d = vault / "howto"; d.mkdir(parents=True)
    p = d / "existing.md"; p.write_text("x", encoding="utf-8")
    _write_cfg(vault, vault_guard_enabled=True, vault_guard_mode="deny")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Edit", "cwd": str(vault), "tool_input": {"file_path": str(p)}}
    assert vg.evaluate(event) is None


def test_templates_carve_out_never_flagged(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True, vault_guard_mode="deny")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "templates" / "new.md")}}
    assert vg.evaluate(event) is None


def test_compaction_plans_carve_out_never_flagged(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True, vault_guard_mode="deny")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "compaction-plans" / "h.md")}}
    assert vg.evaluate(event) is None


def test_unmanaged_root_never_flagged(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True, vault_guard_mode="deny")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "scratch" / "x.md")}}
    assert vg.evaluate(event) is None


def test_non_guarded_tool_never_flagged(monkeypatch, vault):
    _write_cfg(vault, vault_guard_enabled=True, vault_guard_mode="deny")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Read", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "howto" / "new.md")}}
    assert vg.evaluate(event) is None


# --- fail-open / malformed input -----------------------------------------------------

def test_malformed_event_is_noop(monkeypatch, vault):
    _resolve_to(monkeypatch, vault)
    assert vg.evaluate("not a dict") is None
    assert vg.evaluate({}) is None


def test_no_vault_is_noop(monkeypatch):
    _resolve_to(monkeypatch, None)
    event = {"tool_name": "Write", "tool_input": {"file_path": "x.md"}}
    assert vg.evaluate(event) is None


def test_malformed_config_json_is_noop_not_a_crash(monkeypatch, vault):
    mb = vault / ".mb"; mb.mkdir(parents=True)
    (mb / "config.json").write_text("{not json", encoding="utf-8")
    _resolve_to(monkeypatch, vault)
    event = {"tool_name": "Write", "cwd": str(vault),
             "tool_input": {"file_path": str(vault / "howto" / "new.md")}}
    assert vg.evaluate(event) is None      # malformed -> read_config_tolerant -> {} -> disabled


# --- main(): wire contract ---------------------------------------------------------

def _run(monkeypatch, capsys, stdin_text):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    monkeypatch.setattr(vg.sys.stdin, "buffer",
                        io.BytesIO(stdin_text.encode("utf-8")), raising=False)
    rc = vg.main()
    return rc, json.loads(capsys.readouterr().out.strip())


def test_main_noop_emits_empty_object(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    rc, payload = _run(monkeypatch, capsys, json.dumps({"tool_name": "Read"}))
    assert rc == 0 and payload == {}


def test_main_malformed_stdin_never_crashes(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    rc, payload = _run(monkeypatch, capsys, "not json {{{")
    assert rc == 0 and payload == {}


def test_main_evaluate_exception_is_isolated(monkeypatch, vault, capsys):
    _resolve_to(monkeypatch, vault)
    monkeypatch.setattr(vg, "evaluate", lambda event: (_ for _ in ()).throw(RuntimeError("boom")))
    rc, payload = _run(monkeypatch, capsys, json.dumps({"tool_name": "Write"}))
    assert rc == 0 and payload == {}


def test_main_audit_output_end_to_end(monkeypatch, vault, capsys):
    _write_cfg(vault, vault_guard_enabled=True)
    _resolve_to(monkeypatch, vault)
    event = json.dumps({"tool_name": "Write", "cwd": str(vault),
                        "tool_input": {"file_path": str(vault / "howto" / "new.md")}})
    rc, payload = _run(monkeypatch, capsys, event)
    assert rc == 0
    assert "permissionDecision" not in payload["hookSpecificOutput"]
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
