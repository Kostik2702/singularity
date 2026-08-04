"""T14: privacy-safe reflect telemetry -- offline transcript walk, session
population/denominator math, subagent exclusion, malformed-transcript
handling, and the hard privacy line (no prompt/tool-argument content ever
persisted). Fixture shapes below are the REAL, live-machine-verified wire
formats for Claude Code (isSidechain, tool_use/tool_result pending-map) and
Gemini CLI (toolCalls[] with args, activate_skill) -- see
[[SINGULARITY_INCOMING_FORK_IMPLEMENTATION]] T14.
"""
from __future__ import annotations

import json
import os
import time

import pytest

from singularity import reflect
from singularity.config import Config

SECRET = "SECRET_PROMPT_TEXT_MUST_NEVER_APPEAR_IN_OUTPUT"


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path / "vault", data={})


@pytest.fixture(autouse=True)
def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(reflect.Path, "home", classmethod(lambda cls: home))
    # GRK-05: the grok walker honors $GROK_HOME -- a leaked developer env var
    # would point tests at a REAL Grok sessions tree.
    monkeypatch.delenv("GROK_HOME", raising=False)
    return home


def _known_skills():
    return {"mb-search", "mb-memory", "grill"}


# --- Claude transcript fixture builders (real wire shape) ---------------------

def _claude_line(**kw) -> str:
    base = {"type": "assistant", "cwd": r"C:\proj", "isSidechain": False}
    base.update(kw)
    return json.dumps(base)


def _tool_use(tid, name, tool_input, cwd=r"C:\proj", sidechain=False):
    return json.dumps({
        "type": "assistant", "cwd": cwd, "isSidechain": sidechain,
        "message": {"content": [{"type": "tool_use", "id": tid, "name": name, "input": tool_input}]},
    })


def _tool_result(tid, is_error=False, cwd=r"C:\proj", sidechain=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": "ok"}
    if is_error:
        block["is_error"] = True
    return json.dumps({
        "type": "user", "cwd": cwd, "isSidechain": sidechain,
        "message": {"content": [block]},
    })


def _write_claude_session(home, project, session_id, lines, subagent=False):
    if subagent:
        d = home / ".claude" / "projects" / project / "outer-session" / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{session_id}.jsonl"
    else:
        d = home / ".claude" / "projects" / project
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- Gemini transcript fixture builders (real wire shape) ---------------------

def _gemini_meta(session_id):
    return json.dumps({"sessionId": session_id, "projectHash": "x", "startTime": "t", "kind": "chat"})


def _gemini_tool_line(name, args, status="success"):
    return json.dumps({"type": "gemini", "toolCalls": [
        {"id": "1", "name": name, "args": args, "status": status}]})


def _write_gemini_session(home, project, session_id, lines):
    d = home / ".gemini" / "tmp" / project / "chats"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"session-{session_id}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# --- population / discovery: top-level vs subagent -----------------------------

def test_claude_walker_finds_only_top_level_files(_isolate_home, cfg):
    home = _isolate_home
    _write_claude_session(home, "proj", "main-session", [_claude_line(type="user")])
    _write_claude_session(home, "proj", "agent-x", [_claude_line(type="user")], subagent=True)
    root = reflect.claude_transcript_root()
    paths = reflect._claude_session_paths(root, since=0)
    names = {p.stem for p in paths}
    assert names == {"main-session"}


def test_gemini_walker_finds_chat_sessions(_isolate_home, cfg):
    home = _isolate_home
    _write_gemini_session(home, "proj", "2026-07-13T00-00-a", [_gemini_meta("s1")])
    root = reflect.gemini_transcript_root()
    paths = reflect._gemini_session_paths(root, since=0)
    assert len(paths) == 1


def test_claude_parser_skips_sidechain_events_defensively(_isolate_home, cfg):
    # even a top-level-located file with a stray isSidechain:true event must
    # not count it (belt-and-suspenders on top of directory exclusion)
    lines = [
        _tool_use("t1", "Edit", {"file_path": r"C:\proj\a.py"}, sidechain=True),
        _tool_result("t1", sidechain=True),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.code_edits == 0


# --- Claude parser: code edits / vault writes / reads --------------------------

def test_claude_code_edit_counted_outside_vault(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Edit", {"file_path": r"C:\proj\a.py"}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.code_edits == 1
    assert s.vault_writes == 0


def test_claude_vault_write_counted_when_path_inside_vault(_isolate_home, cfg):
    vault_file = str(cfg.vault / "learnings" / "active" / "T.md")
    lines = [
        _tool_use("t1", "Write", {"file_path": vault_file}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.vault_writes == 1
    assert s.code_edits == 0


def test_claude_failed_tool_call_not_counted(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Edit", {"file_path": r"C:\proj\a.py"}),
        _tool_result("t1", is_error=True),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.code_edits == 0


def test_claude_vault_read_counted(_isolate_home, cfg):
    vault_file = str(cfg.vault / "reference" / "R.md")
    lines = [
        _tool_use("t1", "Read", {"file_path": vault_file}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.vault_reads == 1


# --- search_then_read ordering --------------------------------------------------

def test_search_then_read_true_when_search_precedes_read(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Bash", {"command": 'mb search "foo"'}),
        _tool_result("t1"),
        _tool_use("t2", "Read", {"file_path": str(cfg.vault / "reference" / "R.md")}),
        _tool_result("t2"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.searched is True
    assert s.search_then_read is True


def test_search_then_read_false_when_read_precedes_search(_isolate_home, cfg):
    lines = [
        _tool_use("t2", "Read", {"file_path": str(cfg.vault / "reference" / "R.md")}),
        _tool_result("t2"),
        _tool_use("t1", "Bash", {"command": 'mb search "foo"'}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.searched is True
    assert s.search_then_read is False


def test_search_command_requires_word_boundary(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Bash", {"command": "mb searchxyz"}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.searched is False


def test_powershell_search_detected_like_bash(_isolate_home, cfg):
    # T10's real-machine finding: Windows Claude Code fires PowerShell, not
    # just Bash -- reflect's search detection must cover both, same as
    # is_sanctioned_capture_command already does for `mb learn`.
    lines = [
        _tool_use("t1", "PowerShell", {"command": 'mb search "foo"'}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.searched is True


# --- skill invocation ------------------------------------------------------------

def test_claude_skill_invocation_counted_for_known_skill(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Skill", {"skill": "mb-search", "args": SECRET}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.skill_invocations == 1


def test_claude_skill_invocation_not_counted_for_unknown_skill(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Skill", {"skill": "some-foreign-skill", "args": "x"}),
        _tool_result("t1"),
    ]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills())
    assert s.skill_invocations == 0


def test_gemini_skill_invocation_via_activate_skill(_isolate_home, cfg):
    lines = [_gemini_meta("s1"), _gemini_tool_line("activate_skill", {"name": "grill"})]
    path = _write_gemini_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_gemini_session(path, cfg.vault, _known_skills())
    assert s.skill_invocations == 1


def test_gemini_write_and_read_and_search(_isolate_home, cfg):
    vault_file = str(cfg.vault / "reference" / "R.md")
    lines = [
        _gemini_meta("s1"),
        _gemini_tool_line("run_shell_command", {"command": 'mb search "x"'}),
        _gemini_tool_line("read_file", {"file_path": vault_file}),
        _gemini_tool_line("write_file", {"file_path": r"C:\proj\a.py"}),
    ]
    path = _write_gemini_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_gemini_session(path, cfg.vault, _known_skills())
    assert s.searched is True
    assert s.search_then_read is True
    assert s.vault_reads == 1
    assert s.code_edits == 1


def test_gemini_failed_tool_call_not_counted(_isolate_home, cfg):
    lines = [_gemini_meta("s1"), _gemini_tool_line("write_file", {"file_path": r"C:\a.py"}, status="error")]
    path = _write_gemini_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_gemini_session(path, cfg.vault, _known_skills())
    assert s.code_edits == 0


# --- eligible_research / vault_touch properties ---------------------------------

def test_eligible_research_true_for_search_no_edits():
    s = reflect.ReflectSession(client="claude", session_id="s", searched=True)
    assert s.eligible_research is True


def test_eligible_research_false_when_code_edits_present():
    s = reflect.ReflectSession(client="claude", session_id="s", searched=True, code_edits=1)
    assert s.eligible_research is False


def test_eligible_research_false_when_no_activity_at_all():
    s = reflect.ReflectSession(client="claude", session_id="s")
    assert s.eligible_research is False


def test_vault_touch_true_for_read_or_write():
    assert reflect.ReflectSession(client="c", session_id="s", vault_reads=1).vault_touch
    assert reflect.ReflectSession(client="c", session_id="s", vault_writes=1).vault_touch
    assert not reflect.ReflectSession(client="c", session_id="s").vault_touch


# --- numerator/denominator math ------------------------------------------------

def test_rate_none_when_population_is_zero():
    m = reflect._rate([], lambda s: True)
    assert m == {"count": 0, "total": 0, "rate": None}


def test_rate_reflects_denominator_not_just_numerator():
    sessions = [reflect.ReflectSession(client="c", session_id=str(i), code_edits=1 if i < 3 else 0)
               for i in range(10)]
    m = reflect._rate(sessions, lambda s: s.code_edits > 0)
    assert m == {"count": 3, "total": 10, "rate": 0.3}


def test_denominator_includes_adopting_sessions_not_just_non_adopting(_isolate_home, cfg):
    # the population (total_sessions) must count EVERY top-level session,
    # including ones that used mb search -- a denominator that excluded
    # "successful invocations" would make every rate trivially 100%.
    lines = [_tool_use("t1", "Bash", {"command": 'mb search "x"'}), _tool_result("t1")]
    _write_claude_session(_isolate_home, "proj", "s1", lines)
    _write_claude_session(_isolate_home, "proj", "s2", [_claude_line(type="user")])
    result = reflect.reflect(cfg, days=365)
    assert result["total_sessions"] == 2
    assert result["metrics"]["search_then_read"]["total"] == 2


# --- malformed transcript handling ----------------------------------------------

def test_malformed_json_lines_skipped_not_fatal(_isolate_home, cfg):
    d = _isolate_home / ".claude" / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    (d / "bad.jsonl").write_text("{not json\nalso not json\n", encoding="utf-8")
    s = reflect._parse_claude_session(d / "bad.jsonl", cfg.vault, _known_skills())
    assert s.code_edits == 0
    assert s.vault_writes == 0


def test_unreadable_transcript_reports_warning_not_crash(_isolate_home, cfg, monkeypatch):
    d = _isolate_home / ".claude" / "projects" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    missing = d / "gone.jsonl"
    s = reflect._parse_claude_session(missing, cfg.vault, _known_skills())
    assert "unreadable" in " ".join(s.parse_warnings)


def test_reflect_counts_malformed_transcripts_not_silently_drops(_isolate_home, cfg, monkeypatch):
    _write_claude_session(_isolate_home, "proj", "good", [_claude_line(type="user")])
    _write_claude_session(_isolate_home, "proj", "bad", [_claude_line(type="user")])

    def boom(path, vault, known, registry=None):
        if path.stem == "bad":
            raise RuntimeError("simulated parser crash")
        return reflect.ReflectSession(client="claude", session_id="good")
    monkeypatch.setattr(reflect, "_parse_claude_session", boom)
    result = reflect.reflect(cfg, days=365)
    assert result["malformed_transcripts"] == 1
    assert result["total_sessions"] == 1


# --- time window -----------------------------------------------------------------

def test_window_excludes_files_older_than_days(_isolate_home, cfg):
    path = _write_claude_session(_isolate_home, "proj", "old", [_claude_line(type="user")])
    old_time = time.time() - 40 * 86400
    os.utime(path, (old_time, old_time))
    _write_claude_session(_isolate_home, "proj", "fresh", [_claude_line(type="user")])
    result = reflect.reflect(cfg, days=30)
    assert result["total_sessions"] == 1
    assert result["by_client"] == {"claude": 1}


# --- FR-1: reflect uses the same project resolver as every other command ------

def test_claude_project_id_resolved_from_registry(_isolate_home, cfg):
    registry = {"myproj": {"path": r"C:\proj"}}
    lines = [_claude_line(type="user", cwd=r"C:\proj")]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills(), registry)
    assert s.project_id == "myproj"


def test_claude_project_id_none_when_cwd_not_registered(_isolate_home, cfg):
    lines = [_claude_line(type="user", cwd=r"C:\unregistered")]
    path = _write_claude_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_claude_session(path, cfg.vault, _known_skills(), {})
    assert s.project_id is None


def test_gemini_project_id_always_none_documented_limitation(_isolate_home, cfg):
    lines = [_gemini_meta("s1")]
    path = _write_gemini_session(_isolate_home, "proj", "s1", lines)
    s = reflect._parse_gemini_session(path, cfg.vault, _known_skills(), {"any": {"path": "x"}})
    assert s.project_id is None


def test_by_project_aggregation_uses_general_fallback(_isolate_home, cfg):
    cfg.data["projects"] = {"myproj": {"path": r"C:\proj"}}
    _write_claude_session(_isolate_home, "proj", "s1", [_claude_line(type="user", cwd=r"C:\proj")])
    _write_claude_session(_isolate_home, "proj", "s2", [_claude_line(type="user", cwd=r"C:\other")])
    result = reflect.reflect(cfg, days=30)
    assert result["by_project"] == {"myproj": 1, "general": 1}


# --- Codex exclusion --------------------------------------------------------------

def test_codex_is_documented_as_excluded(_isolate_home, cfg):
    result = reflect.reflect(cfg, days=30)
    assert "codex" in result["excluded_clients"]
    assert result["excluded_clients"]["codex"]


# --- privacy: no raw content anywhere in the output -----------------------------

def test_no_skill_args_content_in_output(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Skill", {"skill": "mb-search", "args": SECRET}),
        _tool_result("t1"),
    ]
    _write_claude_session(_isolate_home, "proj", "s1", lines)
    result = reflect.run(cfg, days=30)
    serialized = json.dumps(result, ensure_ascii=False)
    assert SECRET not in serialized


def test_no_command_text_in_output(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Bash", {"command": f'mb search "{SECRET}"'}),
        _tool_result("t1"),
    ]
    _write_claude_session(_isolate_home, "proj", "s1", lines)
    result = reflect.run(cfg, days=30)
    assert SECRET not in json.dumps(result, ensure_ascii=False)


def test_no_transcript_paths_in_output(_isolate_home, cfg):
    path = _write_claude_session(_isolate_home, "proj", "s1", [_claude_line(type="user")])
    result = reflect.run(cfg, days=30)
    serialized = json.dumps(result, ensure_ascii=False)
    assert str(path) not in serialized
    assert "s1" not in serialized   # session id itself is not aggregate-level info


def test_snapshot_file_on_disk_contains_no_secret(_isolate_home, cfg):
    lines = [
        _tool_use("t1", "Skill", {"skill": "mb-search", "args": SECRET}),
        _tool_result("t1"),
    ]
    _write_claude_session(_isolate_home, "proj", "s1", lines)
    reflect.run(cfg, days=30)
    snapshot = (cfg.mb_dir / reflect.SNAPSHOT_NAME).read_text(encoding="utf-8")
    history = (cfg.mb_dir / reflect.HISTORY_NAME).read_text(encoding="utf-8")
    assert SECRET not in snapshot
    assert SECRET not in history


# --- run() persistence: atomic snapshot + bounded history -----------------------

def test_run_writes_atomic_snapshot_no_leftover_tmp(_isolate_home, cfg):
    reflect.run(cfg, days=30)
    assert (cfg.mb_dir / reflect.SNAPSHOT_NAME).exists()
    leftovers = [p for p in cfg.mb_dir.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_run_appends_one_history_row_per_call(_isolate_home, cfg):
    reflect.run(cfg, days=30)
    reflect.run(cfg, days=30)
    rows = (cfg.mb_dir / reflect.HISTORY_NAME).read_text(encoding="utf-8").splitlines()
    assert len(rows) == 2


def test_history_prunes_rows_older_than_retention(_isolate_home, cfg):
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    old_row = {"ts": "2020-01-01T00:00:00", "window_days": 30, "total_sessions": 0,
              "by_client": {}, "metrics": {}}
    (cfg.mb_dir / reflect.HISTORY_NAME).write_text(json.dumps(old_row) + "\n", encoding="utf-8")
    cfg.data["reflect_retention_days"] = 1
    reflect.run(cfg, days=30)
    rows = [json.loads(ln) for ln in
           (cfg.mb_dir / reflect.HISTORY_NAME).read_text(encoding="utf-8").splitlines()]
    assert all(r["ts"] != "2020-01-01T00:00:00" for r in rows)


def test_run_default_days_uses_config(_isolate_home, cfg):
    cfg.data["reflect_window_days"] = 7
    result = reflect.run(cfg)
    assert result["window_days"] == 7


def test_run_explicit_days_overrides_config(_isolate_home, cfg):
    cfg.data["reflect_window_days"] = 7
    result = reflect.run(cfg, days=14)
    assert result["window_days"] == 14


# --- GRK-05: Grok session walker (fixture shapes = tests/fixtures/grok/) --------

def _grok_summary(session_id, cwd, kind=None, parent=None):
    meta = {"info": {"id": session_id, "cwd": cwd}, "created_at": "2026-07-23T18:00:00Z",
            "num_messages": 3, "current_model_id": "m", "chat_format_version": 1}
    if kind:
        meta["session_kind"] = kind
    if parent:
        meta["parent_session_id"] = parent
    return meta


def _grok_call(cid, name, args):
    return json.dumps({"type": "assistant", "content": "", "model_id": "m",
                       "tool_calls": [{"id": cid, "name": name,
                                       "arguments": json.dumps(args)}]})


def _grok_result(cid, content):
    return json.dumps({"type": "tool_result", "tool_call_id": cid, "content": content})


def _write_grok_session(home, session_id, lines, cwd=r"C:\proj", kind=None,
                        parent=None, summary_raw=None):
    d = home / ".grok" / "sessions" / "C%3A%5Cproj" / session_id
    d.mkdir(parents=True, exist_ok=True)
    if summary_raw is not None:
        (d / "summary.json").write_text(summary_raw, encoding="utf-8")
    else:
        (d / "summary.json").write_text(
            json.dumps(_grok_summary(session_id, cwd, kind, parent)), encoding="utf-8")
    (d / "chat_history.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return d


def test_grok_walker_excludes_subagent_sessions(_isolate_home, cfg):
    _write_grok_session(_isolate_home, "sid-top", [_grok_call("c1", "write", {})])
    _write_grok_session(_isolate_home, "sid-child", [_grok_call("c1", "write", {})],
                        kind="subagent")
    result = reflect.reflect(cfg, days=7)
    assert result["by_client"].get("grok") == 1
    assert result["total_sessions"] == 1


def test_grok_fork_counts_as_its_own_top_level_session(_isolate_home, cfg):
    _write_grok_session(_isolate_home, "sid-a", ['{"type":"assistant","content":"hi"}'])
    _write_grok_session(_isolate_home, "sid-b", ['{"type":"assistant","content":"hi"}'],
                        parent="sid-a")
    result = reflect.reflect(cfg, days=7)
    assert result["by_client"].get("grok") == 2


def test_grok_malformed_summary_counts_malformed_not_population(_isolate_home, cfg):
    _write_grok_session(_isolate_home, "sid-bad", ['{"type":"assistant","content":"x"}'],
                        summary_raw="{ broken json")
    result = reflect.reflect(cfg, days=7)
    assert result["by_client"].get("grok") is None
    assert result["malformed_transcripts"] == 1


def test_grok_walker_honors_grok_home_env(_isolate_home, cfg, monkeypatch, tmp_path):
    custom = tmp_path / "custom-grok"
    d = custom / "sessions" / "grp" / "sid-env"
    d.mkdir(parents=True)
    (d / "summary.json").write_text(json.dumps(_grok_summary("sid-env", r"C:\proj")),
                                    encoding="utf-8")
    (d / "chat_history.jsonl").write_text('{"type":"assistant","content":"hi"}\n',
                                          encoding="utf-8")
    monkeypatch.setenv("GROK_HOME", str(custom))
    result = reflect.reflect(cfg, days=7)
    assert result["by_client"].get("grok") == 1


def test_grok_window_excludes_old_sessions(_isolate_home, cfg):
    d = _write_grok_session(_isolate_home, "sid-old",
                            ['{"type":"assistant","content":"hi"}'])
    old = time.time() - 30 * 86400
    os.utime(d / "chat_history.jsonl", (old, old))
    result = reflect.reflect(cfg, days=7)
    assert result["by_client"].get("grok") is None


def test_grok_code_edit_and_vault_write_boundaries(_isolate_home, cfg):
    vault = str(cfg.vault / "note.md")
    lines = [
        _grok_call("c1", "write", {"file_path": r"C:\proj\a.py", "content": "x"}),
        _grok_result("c1", "The file C:\\proj\\a.py has been created."),
        _grok_call("c2", "write", {"file_path": vault, "content": "x"}),
        _grok_result("c2", "The file has been created."),
    ]
    _write_grok_session(_isolate_home, "sid-1", lines)
    result = reflect.reflect(cfg, days=7)
    assert result["metrics"]["code_edits"]["count"] == 1
    assert result["metrics"]["vault_touch"]["count"] == 1


def test_grok_failed_tool_calls_not_counted(_isolate_home, cfg):
    lines = [
        _grok_call("c1", "write", {"file_path": r"C:\proj\a.py", "content": "x"}),
        _grok_result("c1", "Tool `write` failed: IO Error: failed to create whole tree"),
        _grok_call("c2", "write", {"file_path": r"C:\proj\b.py", "content": "x"}),
        _grok_result("c2", "Failed to parse arguments for tool `write`: missing field"),
    ]
    _write_grok_session(_isolate_home, "sid-1", lines)
    result = reflect.reflect(cfg, days=7)
    assert result["metrics"]["code_edits"]["count"] == 0


def test_grok_search_then_read_requires_exit0_and_order(_isolate_home, cfg):
    vault_file = str(cfg.vault / "n.md")
    ok = [
        _grok_call("c1", "run_terminal_command", {"command": "mb search \"q\"", "description": "d"}),
        _grok_result("c1", "exit: 0\nresults\n"),
        _grok_call("c2", "read_file", {"file_path": vault_file}),
        _grok_result("c2", "note content"),
    ]
    _write_grok_session(_isolate_home, "sid-ok", ok)
    bad = [
        _grok_call("c1", "run_terminal_command", {"command": "mb search \"q\"", "description": "d"}),
        _grok_result("c1", "exit: 1\nboom\n"),
        _grok_call("c2", "read_file", {"file_path": vault_file}),
        _grok_result("c2", "note content"),
    ]
    _write_grok_session(_isolate_home, "sid-bad", bad)
    result = reflect.reflect(cfg, days=7)
    assert result["metrics"]["search_then_read"]["count"] == 1
    assert result["metrics"]["vault_touch"]["count"] == 2   # vault READS still count


def test_grok_project_id_from_verified_summary_cwd(_isolate_home, cfg):
    d = _write_grok_session(_isolate_home, "sid-1",
                            ['{"type":"assistant","content":"hi"}'])
    registry = {"proj-x": {"path": r"C:\proj"}}
    s = reflect._parse_grok_session(d, cfg.vault, set(), registry)
    assert s.project_id == "proj-x"


def test_grok_project_id_none_when_cwd_not_registered(_isolate_home, cfg):
    d = _write_grok_session(_isolate_home, "sid-1",
                            ['{"type":"assistant","content":"hi"}'])
    s = reflect._parse_grok_session(d, cfg.vault, set(), {})
    assert s.project_id is None


def test_grok_skill_invocations_never_counted_documented_limitation(_isolate_home, cfg):
    lines = ['{"type":"assistant","content":"using mb-search skill now"}']
    _write_grok_session(_isolate_home, "sid-1", lines)
    result = reflect.reflect(cfg, days=7)
    assert result["metrics"]["skill_invocation"]["count"] == 0


def test_grok_secret_prompt_text_never_in_snapshot(_isolate_home, cfg):
    lines = [
        json.dumps({"type": "user", "content": [{"type": "text", "text": SECRET}],
                    "prompt_index": 0}),
        _grok_call("c1", "write", {"file_path": r"C:\proj\a.py", "content": SECRET}),
        _grok_result("c1", "The file has been created."),
    ]
    _write_grok_session(_isolate_home, "sid-1", lines)
    result = reflect.run(cfg, days=7)
    snapshot = (cfg.mb_dir / reflect.SNAPSHOT_NAME).read_text(encoding="utf-8")
    history = (cfg.mb_dir / reflect.HISTORY_NAME).read_text(encoding="utf-8")
    for blob in (json.dumps(result), snapshot, history):
        assert SECRET not in blob
        assert "sid-1" not in blob            # session ids never serialized
        assert "C%3A%5Cproj" not in blob      # encoded paths never serialized
