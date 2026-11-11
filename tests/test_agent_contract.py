"""SubagentStop contract: transcripts are built as temp JSONL in the real shape."""
import io
import json

import pytest

from singularity.hooks import subagent_stop
from singularity.machine import agent_contract as ac

PREAMBLE = "/home/u/.claude/agents/AGENT_SHARED_PREAMBLE.md"


def _use(name, **inp):
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": name, "input": inp}]}}


def _write(tmp_path, events, raw_lines=(), name="agent.jsonl"):
    p = tmp_path / name
    lines = [json.dumps({"type": "user", "message": {"content": "the brief"}})]
    lines += [json.dumps(e) for e in events] + list(raw_lines)
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _full(report="done. 92% — because tests ran"):
    return [_use("Read", file_path=PREAMBLE), _use("Bash", command='mb search "x"'),
            _use("Skill", skill="confidence-calibration"),
            _use("SubagentHandback", message=report)]


def test_investigator_complete(tmp_path):
    assert ac.missing("investigator", _write(tmp_path, _full())) == []


def test_confidence_needs_skill_and_summary_line(tmp_path):
    no_skill = [e for e in _full() if e["message"]["content"][0]["name"] != "Skill"]
    assert ac.missing("investigator", _write(tmp_path, no_skill)) == ["confidence"]
    no_line = _full("done, fairly sure")
    assert ac.missing("investigator", _write(tmp_path, no_line)) == ["confidence"]
    assert ac.missing("investigator", _write(tmp_path, _full("90% - because x"))) == []


def test_last_message_argument_counts_as_report(tmp_path):
    ev = _full("no line here")
    assert ac.missing("critic", _write(tmp_path, ev), "88% - because y") == []


def test_mb_search_only_as_a_command(tmp_path):
    base = [_use("Read", file_path=PREAMBLE), _use("Skill", skill="confidence-calibration"),
            _use("SubagentHandback", message="91% - because z")]
    quoted = [_use("Bash", command='echo "run mb search later"')] + base
    assert ac.missing("reviewer", _write(tmp_path, quoted)) == ["memory_search"]
    cd = [_use("PowerShell", command='cd /x && mb search "q"')] + base
    assert ac.missing("reviewer", _write(tmp_path, cd)) == []


def test_diff_after_edit_order(tmp_path):
    pre = _use("Read", file_path=PREAMBLE)
    ok = [pre, _use("Edit", file_path="a"), _use("Bash", command="git diff --stat")]
    assert ac.missing("coder", _write(tmp_path, ok)) == []
    bad = [pre, _use("Bash", command="git diff --stat"), _use("Edit", file_path="a")]
    assert ac.missing("coder", _write(tmp_path, bad)) == ["diff_after_edit"]
    assert ac.missing("coder", _write(tmp_path, [pre])) == []


def test_fail_open(tmp_path):
    p = _write(tmp_path, [])
    assert ac.missing("general-purpose", p) == []
    assert ac.missing("", p) == []
    # garbage lines are skipped, the rest still judged: a readable file is a verdict
    p = _write(tmp_path, [_use("Read", file_path=PREAMBLE)], raw_lines=["{not json", "[1]", '"x"'])
    assert ac.missing("web-viewer", p) == []
    assert ac.missing("investigator", p) == ["memory_search", "confidence"]


def test_no_verdict_is_none_not_empty(tmp_path, monkeypatch):
    assert ac.missing("investigator", tmp_path / "absent.jsonl") is None
    assert ac.missing("investigator", "") is None
    p = _write(tmp_path, _full())
    assert ac.missing("investigator", p) == []
    monkeypatch.setitem(ac.CHECKS, "confidence", lambda run: 1 / 0)
    assert ac.missing("investigator", p) is None


def test_contract_names_are_all_known():
    for agent, names in ac.CONTRACTS.items():
        for name in names:
            assert name in ac.CHECKS and name in ac.HOW, (agent, name)


def test_hook_logs_error_when_a_contract_name_is_unknown(vault, tmp_path, monkeypatch, capsys):
    monkeypatch.setitem(ac.CONTRACTS, "reviewer", ("preamble", "memory-search"))
    clean = _write(tmp_path, _full(), name="clean.jsonl")
    ev = _event(tmp_path, agent_type="reviewer", agent_transcript_path=str(clean))
    assert _hook(monkeypatch, capsys, ev) == ""
    (entry,) = _log(vault)
    assert entry["verdict"] == "error" and entry["missing"] == [] and entry["blocked"] is False


def test_hook_without_transcript_path_logs_error(vault, tmp_path, monkeypatch, capsys):
    ev = _event(tmp_path)
    del ev["agent_transcript_path"]
    assert _hook(monkeypatch, capsys, ev) == ""
    (entry,) = _log(vault)
    assert entry["verdict"] == "error" and entry["blocked"] is False


@pytest.mark.parametrize("command,ok", [
    ("git diff --stat", True),
    ("git -C /a/b diff --stat", True),
    ("git --no-pager diff", True),
    ("git -c color.ui=never status", True),
    ("cd x && git status --short", True),
    ('grep "gitdiff" f', False),
    ("echo digit status", False),
])
def test_git_view_matcher(tmp_path, command, ok):
    ev = [_use("Edit", file_path="a"), _use("Bash", command=command)]
    assert (ac.missing("coder", _write(tmp_path, [_use("Read", file_path=PREAMBLE), *ev]))
            == []) is ok


def test_write_outside_cwd_after_diff_is_fine(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    pre = _use("Read", file_path=PREAMBLE)
    seq = [pre, _use("Edit", file_path=str(repo / "a.py")), _use("Bash", command="git diff"),
           _use("Write", file_path=str(tmp_path / "scratch" / "note.md"))]
    assert ac.missing("coder", _write(tmp_path, seq), cwd=str(repo)) == []
    inside = seq[:3] + [_use("Write", file_path=str(repo / "b.py"))]
    assert ac.missing("coder", _write(tmp_path, inside, name="i.jsonl"),
                      cwd=str(repo)) == ["diff_after_edit"]
    assert ac.missing("coder", _write(tmp_path, seq, name="n.jsonl")) == ["diff_after_edit"]


@pytest.mark.parametrize("command,ok", [
    ('mb search "q"', True),
    ("  mb search q", True),
    ("cd /x && mb search q", True),
    ("timeout 30 mb search q", True),
    ("MB_HOME=/v mb search q", True),
    ("mb --vault /v search q", True),
    ("/Users/u/.local/bin/mb search q", True),
    ("~/.local/bin/mb search q", True),
    ('for q in a b; do mb search "$q"; done', True),
    ("{ mb search q; }", True),
    ('bash -lc "mb search q"', True),
    ("x=$(mb search q)", True),
    ('echo "run mb search later"', False),
    ('grep -r "mb search" docs/', False),
    ("mb searchx", False),
])
def test_mb_search_matcher(command, ok):
    assert (ac._MB_SEARCH.search(command) is not None) is ok


@pytest.mark.parametrize("line,ok", [
    ("92% \u2014 because x", True),
    ("92 % \u2014 \u043f\u043e\u0442\u043e\u043c\u0443 \u0447\u0442\u043e x", True),
    ("**92%** \u2014 because", True),
    ("92% \u2014 **because**", True),
    ("92% - because", True),
    ("92% -- because", True),
    ("92% \u2013 because", True),
    ("92% (because x)", True),
    ("92% because x", True),
    ("Confidence: 85% \u2014 \u0442\u0430\u043a \u043a\u0430\u043a x", True),
    ("85% \u2014 \u0442.\u043a. x", True),
    ("92% of tests pass", False),
    ("coverage 80% \u2014 fine", False),
])
def test_summary_line_matcher(line, ok):
    assert (ac._SUMMARY_LINE.search(line) is not None) is ok


def test_plugin_namespaced_skill_counts(tmp_path):
    ev = [_use("Read", file_path=PREAMBLE), _use("Bash", command="mb search x"),
          _use("Skill", skill="singularity:confidence-calibration"),
          _use("SubagentHandback", message="90% - because x")]
    assert ac.missing("investigator", _write(tmp_path, ev)) == []


def test_reason_text():
    r = ac.reason(["preamble", "confidence"])
    lines = r.split("\n")
    assert lines[0].startswith("[Singularity contract] Your report is not accepted yet: 2 ")
    assert lines[1].startswith("1. Read the shared preamble")
    assert lines[2].startswith("2. Compute confidence")
    assert lines[-1] == "This check runs once; it will not stop you a second time."


@pytest.fixture
def vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / ".mb").mkdir(parents=True)
    import singularity.config as cfg
    monkeypatch.setattr(cfg, "resolve_vault", lambda *_a, **_k: v)
    monkeypatch.setattr(cfg, "read_config_tolerant", lambda: {})
    return v


def _hook(monkeypatch, capsys, event):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps(event).encode())))
    rc = subagent_stop.main()
    assert rc == 0
    return capsys.readouterr().out


def _event(tmp_path, **over):
    ev = {"agent_type": "investigator", "agent_id": "a1", "session_id": "s1",
          "agent_transcript_path": str(_write(tmp_path, [_use("Read", file_path=PREAMBLE)]))}
    ev.update(over)
    return ev


def _log(vault):
    p = vault / ".mb" / "agent-contract.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


def test_hook_blocks_and_logs(vault, tmp_path, monkeypatch, capsys):
    out = _hook(monkeypatch, capsys, _event(tmp_path))
    data = json.loads(out)
    assert data["decision"] == "block"
    assert "confidence-calibration" in data["reason"] and "mb search" in data["reason"]
    (entry,) = _log(vault)
    assert entry["blocked"] is True and entry["missing"] == ["memory_search", "confidence"]
    assert entry["agent_type"] == "investigator" and entry["session"] == "s1"


def test_hook_stop_hook_active_allows_silently(vault, tmp_path, monkeypatch, capsys):
    out = _hook(monkeypatch, capsys, _event(tmp_path, stop_hook_active=True))
    assert out == ""
    (entry,) = _log(vault)
    assert entry["retry"] is True and entry["blocked"] is False
    assert entry["verdict"] == "missing" and entry["missing"] == ["memory_search", "confidence"]


def test_hook_enforce_off_logs_only(vault, tmp_path, monkeypatch, capsys):
    import singularity.config as cfg
    monkeypatch.setattr(cfg, "read_config_tolerant", lambda: {"agent_contract_enforce": False})
    out = _hook(monkeypatch, capsys, _event(tmp_path))
    assert out == ""
    (entry,) = _log(vault)
    assert entry["blocked"] is False and entry["missing"] == ["memory_search", "confidence"]


def test_hook_logs_clean_run_but_not_foreign_agent(vault, tmp_path, monkeypatch, capsys):
    clean = _write(tmp_path, _full(), name="clean.jsonl")
    assert _hook(monkeypatch, capsys, _event(tmp_path, agent_transcript_path=str(clean))) == ""
    assert _hook(monkeypatch, capsys, _event(tmp_path, agent_type="Explore")) == ""
    (entry,) = _log(vault)
    assert entry["missing"] == [] and entry["blocked"] is False and entry["verdict"] == "ok"


def test_hook_garbage_stdin_is_silent(vault, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"not json")))
    assert subagent_stop.main() == 0
    assert capsys.readouterr().out == ""


def test_preamble_read_through_the_shell_counts(tmp_path):
    """Most real agents `cat` the preamble instead of using Read."""
    shell = _write(tmp_path, [_use("Bash", command="cat ~/.claude/agents/AGENT_SHARED_PREAMBLE.md")])
    assert ac.missing("scout", shell) == []
    other = _write(tmp_path, [_use("Bash", command="cat ~/.claude/agents/coder.md")], name="o.jsonl")
    assert ac.missing("scout", other) == ["preamble"]
