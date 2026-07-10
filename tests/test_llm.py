"""Unit tests for mb llm — no real server/binary required."""
import http.server
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity import llm
from singularity.config import Config


def _llm(tmp_path, extra=None):
    gguf = tmp_path / "model.gguf"
    server = tmp_path / "llama-server"
    cfg = {
        "server_bin": str(server),
        "server_host": "127.0.0.1",
        "server_port": 8080,
        "models": {
            "qwen": {"gguf": str(gguf), "args": ["-ngl", "99", "--jinja"]},
            "plain": {"gguf": str(gguf), "args": ["-ngl", "10"]},
        },
    }
    if extra:
        cfg.update(extra)
    return cfg, server, gguf


def test_build_server_argv_injects_bin_gguf_host_port_and_args(tmp_path):
    cfg, server, gguf = _llm(tmp_path)
    argv = llm.build_server_argv(cfg, "qwen")
    assert argv == [str(server), "-m", str(gguf),
                    "--host", "127.0.0.1", "--port", "8080",
                    "-ngl", "99", "--jinja"]


def test_build_server_argv_unknown_model_raises(tmp_path):
    cfg, _, _ = _llm(tmp_path)
    with pytest.raises(llm.LLMConfigError):
        llm.build_server_argv(cfg, "nope")


def test_build_server_argv_missing_server_bin_raises(tmp_path):
    cfg, _, _ = _llm(tmp_path)
    del cfg["server_bin"]
    with pytest.raises(llm.LLMConfigError):
        llm.build_server_argv(cfg, "qwen")


def test_validate_paths_missing_files_raise_then_pass(tmp_path):
    cfg, server, gguf = _llm(tmp_path)
    with pytest.raises(llm.LLMConfigError):
        llm.validate_paths(cfg, "qwen")           # neither file exists yet
    server.write_text("x"); gguf.write_text("x")
    llm.validate_paths(cfg, "qwen")               # now both exist -> no raise


def test_has_chat_template(tmp_path):
    cfg, _, _ = _llm(tmp_path)
    assert llm.has_chat_template(cfg, "qwen") is True
    assert llm.has_chat_template(cfg, "plain") is False


def test_llm_cfg_missing_section_raises(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    with pytest.raises(llm.LLMConfigError):
        llm._llm_cfg(cfg)


def test_state_round_trip(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    assert llm.load_state(cfg) is None
    llm.write_state(cfg, {"model": "qwen", "server_pid": 111})
    assert llm.state_path(cfg) == tmp_path / ".mb" / "llm.state.json"
    assert llm.load_state(cfg)["server_pid"] == 111
    llm.clear_state(cfg)
    assert llm.load_state(cfg) is None
    llm.clear_state(cfg)  # idempotent, no raise


def test_load_state_corrupt_returns_none(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    p = llm.state_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json", encoding="utf-8")
    assert llm.load_state(cfg) is None


def test_log_path(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    assert llm._log(cfg, "server") == tmp_path / ".mb" / "llm-server.log"


class _Handler(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.__class__.status)
        self.end_headers()

    def log_message(self, *a):  # silence
        pass


def _serve(status):
    _Handler.status = status
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, srv.server_address[1]


def test_poll_health_true_on_200():
    srv, port = _serve(200)
    try:
        assert llm.poll_health(f"http://127.0.0.1:{port}/health", 2, interval=0.05) is True
    finally:
        srv.shutdown()


def test_poll_health_false_on_persistent_503():
    srv, port = _serve(503)
    try:
        assert llm.poll_health(f"http://127.0.0.1:{port}/health", 0.3, interval=0.05) is False
    finally:
        srv.shutdown()


def test_poll_health_false_on_connection_refused():
    # nothing listening on this port
    assert llm.poll_health("http://127.0.0.1:9/health", 0.3, interval=0.05) is False


def test_poll_health_returns_false_fast_when_process_dead(monkeypatch):
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.poll_health("http://127.0.0.1:9/health", timeout_s=5,
                           interval=0.05, pid=123) is False


def test_poll_health_true_when_process_alive_and_server_healthy(monkeypatch):
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: True)
    srv, port = _serve(200)
    try:
        assert llm.poll_health(f"http://127.0.0.1:{port}/health", 2,
                               interval=0.05, pid=123) is True
    finally:
        srv.shutdown()


def _ready_llm(tmp_path, with_router=False):
    cfg, server, gguf = _llm(tmp_path)
    server.write_text("x"); gguf.write_text("x")   # pass validate_paths
    if with_router:
        cfg["router"] = {"cmd": ["ccr", "start"], "host": "127.0.0.1",
                         "port": 3456, "ready_path": "/health"}
    return Config(vault=tmp_path, data={"llm": cfg})


def test_start_server_only_writes_state_and_prints_openai_url(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    spawned = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 0
    state = llm.load_state(cfg)
    assert state["model"] == "qwen" and state["server_pid"] == 4242
    assert state["router_pid"] is None
    out = capsys.readouterr().out
    assert "127.0.0.1:8080" in out
    assert len(spawned) == 1                       # server only


def test_start_with_router_spawns_both_in_order_and_prints_base_url(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path, with_router=True)
    order = []
    monkeypatch.setattr(llm.proc, "spawn_detached",
                        lambda argv, log: order.append(argv[0]) or (5000 + len(order)))
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 0
    # server binary spawned before router command
    assert order[0].endswith("llama-server") and order[1] == "ccr"
    state = llm.load_state(cfg)
    assert state["router_pid"] == 5002
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:3456" in capsys.readouterr().out


def test_start_server_health_timeout_kills_and_clears(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    killed = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 7777)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 1
    assert 7777 in killed
    assert llm.load_state(cfg) is None


def test_start_switch_stops_existing_first(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    llm.write_state(cfg, {"model": "old", "server_pid": 999,
                          "router_pid": None, "server_port": 8080})
    events = []
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: pid == 999)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: events.append(("kill", pid)))
    monkeypatch.setattr(llm.proc, "spawn_detached",
                        lambda argv, log: events.append(("spawn", argv)) or 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    assert llm.start(cfg, "qwen") == 0
    kill_idx = next(i for i, e in enumerate(events) if e == ("kill", 999))
    spawn_idx = next(i for i, e in enumerate(events) if e[0] == "spawn")
    assert kill_idx < spawn_idx                      # old server killed before new spawn
    assert llm.load_state(cfg)["server_pid"] == 4242


def test_start_switch_reaps_live_router_when_server_dead(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    llm.write_state(cfg, {"model": "old", "server_pid": 999, "router_pid": 888,
                          "server_port": 8080})
    killed = []
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: pid == 888)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    assert llm.start(cfg, "qwen") == 0
    assert 888 in killed                              # old (live) router reaped via stop
    assert llm.load_state(cfg)["server_pid"] == 4242


def test_start_missing_gguf_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    Path(cfg.get("llm")["models"]["qwen"]["gguf"]).unlink()   # remove gguf
    assert llm.start(cfg, "qwen") == 1
    assert "gguf not found" in capsys.readouterr().err


def test_start_server_spawn_error_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    def _raise(argv, log):
        raise OSError("boom")
    monkeypatch.setattr(llm.proc, "spawn_detached", _raise)
    assert llm.start(cfg, "qwen") == 1
    assert "failed to start server" in capsys.readouterr().err
    assert llm.load_state(cfg) is None


def test_start_router_missing_cmd_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path, with_router=True)
    del cfg.get("llm")["router"]["cmd"]
    killed = []
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 5000)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    assert llm.start(cfg, "qwen") == 1
    assert "failed to start router" in capsys.readouterr().err
    assert 5000 in killed
    assert llm.load_state(cfg) is None


def test_stop_not_running(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={})
    assert llm.stop(cfg) == 0
    assert "not running" in capsys.readouterr().out


def test_stop_kills_router_then_server_and_clears(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={})
    llm.write_state(cfg, {"model": "q", "server_pid": 11, "router_pid": 22})
    order = []
    monkeypatch.setattr(llm.proc, "kill", lambda pid: order.append(pid))
    assert llm.stop(cfg) == 0
    assert order == [22, 11]                        # router before server
    assert llm.load_state(cfg) is None
    assert "stopped" in capsys.readouterr().out


def test_status_reports_and_flags_dead(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={})
    llm.write_state(cfg, {"model": "q", "server_pid": 11, "server_port": 8080,
                          "router_pid": None, "started_at": "2026-07-08T14:00:00"})
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.status(cfg) == 0
    out = capsys.readouterr().out
    assert "q" in out and "DEAD" in out


def test_status_not_running(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={})
    assert llm.status(cfg) == 0
    assert "not running" in capsys.readouterr().out


def test_list_models_marks_active(tmp_path, capsys):
    inner, server, gguf = _llm(tmp_path)
    cfg = Config(vault=tmp_path, data={"llm": inner})
    llm.write_state(cfg, {"model": "qwen"})
    assert llm.list_models(cfg) == 0
    out = capsys.readouterr().out
    assert "qwen" in out and "(active)" in out and "plain" in out


def test_logs_tail(tmp_path, capsys):
    cfg = Config(vault=tmp_path, data={})
    log = llm._log(cfg, "server")
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("l1\nl2\nl3\n", encoding="utf-8")
    assert llm.logs(cfg, "server", n=2) == 0
    out = capsys.readouterr().out
    assert "l2" in out and "l3" in out and "l1" not in out


from singularity import cli


def test_cli_llm_status_dispatches(tmp_path, monkeypatch, capsys):
    # point the CLI at an isolated vault and stub config loading
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    tmp_path.joinpath(".mb").mkdir(parents=True, exist_ok=True)
    rc = cli.main(["llm", "status"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_cli_llm_start_parses_model_arg(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    seen = {}
    monkeypatch.setattr("singularity.llm.start",
                        lambda c, model: seen.update(model=model) or 0)
    assert cli.main(["llm", "start", "qwen27b"]) == 0
    assert seen["model"] == "qwen27b"


# --- mb llm code -----------------------------------------------------------
import types


def test_resolve_auth_token_chain(tmp_path):
    # env wins over config; config wins over default
    assert llm.resolve_auth_token({"auth_token": "cfg"},
                                  {"ANTHROPIC_AUTH_TOKEN": "env"}) == "env"
    assert llm.resolve_auth_token({"auth_token": "cfg"}, {}) == "cfg"
    assert llm.resolve_auth_token({}, {}) == "local"
    assert llm.resolve_auth_token({"auth_token": ""}, {}) == "local"  # empty -> default


def _code_ready(tmp_path, monkeypatch, alive=True):
    cfg = Config(vault=tmp_path, data={"llm": {
        "server_bin": "x", "server_host": "127.0.0.1", "server_port": 8080,
        "models": {},
        # keep isolated-profile generation off the real ~/.claude in tests
        "claude_home": str(tmp_path / "home"),
        "code_config_dir": str(tmp_path / "iso")}})
    llm.write_state(cfg, {"model": "qwythos9b", "server_pid": 111,
                          "server_port": 8080})
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: alive)
    return cfg


def test_code_no_state_returns_1(tmp_path, monkeypatch, capsys):
    cfg = Config(vault=tmp_path, data={"llm": {"server_bin": "x", "models": {}}})
    assert llm.code(cfg) == 1
    assert "mb llm start" in capsys.readouterr().err


def test_code_dead_server_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _code_ready(tmp_path, monkeypatch, alive=False)
    assert llm.code(cfg) == 1
    assert "mb llm start" in capsys.readouterr().err


def test_code_missing_claude_cli_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    assert llm.code(cfg) == 1
    assert "npm install -g @anthropic-ai/claude-code" in capsys.readouterr().err


def test_code_runs_claude_with_env_overlay_and_passthrough(tmp_path, monkeypatch):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    calls = {}

    def fake_run(argv, env=None):
        calls["argv"], calls["env"] = argv, env
        return types.SimpleNamespace(returncode=7)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    rc = llm.code(cfg, ["--", "--resume"])       # leading REMAINDER "--" stripped
    assert rc == 7                               # claude's exit code propagated
    assert calls["argv"] == ["/bin/claude", "--resume"]
    env = calls["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8080"   # bare origin
    assert env["ANTHROPIC_AUTH_TOKEN"] == "local"
    assert env["ANTHROPIC_MODEL"] == "qwythos9b"
    assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert "PATH" in env                         # os.environ inherited


def test_code_env_token_not_clobbered(tmp_path, monkeypatch):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "user-token")
    seen = {}
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda argv, env=None: seen.update(env=env)
                        or types.SimpleNamespace(returncode=0))
    assert llm.code(cfg) == 0
    assert seen["env"]["ANTHROPIC_AUTH_TOKEN"] == "user-token"


def test_code_strips_inherited_anthropic_api_key(tmp_path, monkeypatch):
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-reach-child")
    seen = {}
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda argv, env=None: seen.update(env=env)
                        or types.SimpleNamespace(returncode=0))
    assert llm.code(cfg) == 0
    assert "ANTHROPIC_API_KEY" not in seen["env"]


# --- isolated CLAUDE_CONFIG_DIR profile -------------------------------------


def _home_with_config(tmp_path):
    home = tmp_path / "home"
    (home / "skills" / "mb-search").mkdir(parents=True)
    (home / "agents").mkdir()
    (home / "CLAUDE.md").write_text("global instructions", encoding="utf-8")
    (home / "settings.json").write_text(json.dumps({
        "permissions": {"allow": ["Bash"]},
        "theme": "dark",
        "statusLine": {"type": "command"},
        "hooks": {"SessionStart": [{"hooks": [{"command": "x"}]}]},
        "enabledPlugins": {"superpowers@x": True},
        "mcpServers": {"ctx7": {"command": "y"}},
    }), encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "mcpServers": {"ctx7": {}}}),
        encoding="utf-8")
    return home


def test_build_isolated_config_drops_breakers_keeps_rest(tmp_path):
    home = _home_with_config(tmp_path)
    dst = tmp_path / "iso"
    llm.build_isolated_config(home, dst)
    settings = json.loads((dst / "settings.json").read_text(encoding="utf-8"))
    assert "hooks" not in settings           # the jinja breaker
    assert "enabledPlugins" not in settings  # Claude-only
    assert "mcpServers" not in settings      # off by user choice
    assert settings["permissions"] == {"allow": ["Bash"]}   # carried
    assert settings["theme"] == "dark"
    assert settings["statusLine"] == {"type": "command"}
    md = (dst / "CLAUDE.md").read_text(encoding="utf-8")
    assert md == llm.MINIMAL_CLAUDE_MD                       # minimal stub, NOT the full home CLAUDE.md
    assert "global instructions" not in md                  # full home CLAUDE.md not carried
    assert "MANDATORY" not in md                             # no imperative that loops a small model
    assert (dst / "skills" / "mb-search").is_dir()          # mirrored for manual memory
    assert (dst / "agents").is_dir()
    dot = json.loads((dst / ".claude.json").read_text(encoding="utf-8"))
    assert dot["hasCompletedOnboarding"] is True            # onboarding carried
    assert "mcpServers" not in dot                          # but MCP stripped


def test_build_isolated_config_tolerates_missing_home(tmp_path):
    dst = tmp_path / "iso"
    llm.build_isolated_config(tmp_path / "nope", dst)        # no source files
    assert json.loads((dst / "settings.json").read_text(encoding="utf-8")) == {}
    assert (dst / "CLAUDE.md").read_text(encoding="utf-8") == llm.MINIMAL_CLAUDE_MD


def test_build_isolated_config_uses_claude_md_override(tmp_path):
    home = _home_with_config(tmp_path)
    override = tmp_path / "my_local_CLAUDE.md"
    override.write_text("custom local instructions", encoding="utf-8")
    dst = tmp_path / "iso"
    llm.build_isolated_config(home, dst, str(override))
    assert (dst / "CLAUDE.md").read_text(encoding="utf-8") == "custom local instructions"


def test_build_isolated_config_ignores_missing_override(tmp_path):
    dst = tmp_path / "iso"
    llm.build_isolated_config(tmp_path / "nope", dst, str(tmp_path / "absent.md"))
    assert (dst / "CLAUDE.md").read_text(encoding="utf-8") == llm.MINIMAL_CLAUDE_MD


def test_code_sets_claude_config_dir(tmp_path, monkeypatch):
    _home_with_config(tmp_path)
    cfg = _code_ready(tmp_path, monkeypatch)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    seen = {}
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda argv, env=None: seen.update(env=env)
                        or types.SimpleNamespace(returncode=0))
    assert llm.code(cfg) == 0
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(tmp_path / "iso")
    assert (tmp_path / "iso" / "settings.json").exists()     # profile materialized


def test_cli_llm_code_dispatches_with_remainder(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    seen = {}
    monkeypatch.setattr("singularity.llm.code",
                        lambda c, extra: seen.update(extra=extra) or 0)
    assert cli.main(["llm", "code", "--", "--resume"]) == 0
    assert seen["extra"] == ["--", "--resume"]


def test_start_prints_code_hint(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 0
    assert "mb llm code" in capsys.readouterr().out


# --- mb llm setup: args sanity ---------------------------------------------

def test_fix_model_args_bare_fa_before_flag():
    fixed, fixes = llm.fix_model_args(["-ngl", "99", "-fa", "--jinja"])
    assert fixed == ["-ngl", "99", "-fa", "on", "--jinja"]
    assert fixes == ["-fa -> -fa on"]


def test_fix_model_args_bare_fa_last():
    fixed, fixes = llm.fix_model_args(["-fa"])
    assert fixed == ["-fa", "on"] and len(fixes) == 1


def test_fix_model_args_valued_fa_untouched():
    for val in ("on", "off", "auto"):
        fixed, fixes = llm.fix_model_args(["-fa", val, "--jinja"])
        assert fixed == ["-fa", val, "--jinja"] and fixes == []


def test_fix_model_args_long_form_and_no_fa():
    assert llm.fix_model_args(["--flash-attn", "--jinja"])[0] == \
        ["--flash-attn", "on", "--jinja"]
    args = ["-ngl", "99", "--jinja"]
    assert llm.fix_model_args(args) == (args, [])


# --- mb llm setup: release asset selection ----------------------------------

def _asset(name):
    return {"name": name, "browser_download_url": f"https://gh/{name}"}


def test_pick_release_assets_single_match_with_cudart():
    assets = [_asset("llama-b9999-bin-win-cpu-x64.zip"),
              _asset("llama-b9999-bin-win-cuda-12.4-x64.zip"),
              _asset("cudart-llama-bin-win-cuda-12.4-x64.zip"),
              _asset("llama-b9999-bin-macos-arm64.zip")]
    main, cudart = llm.pick_release_assets(assets)
    assert main["name"] == "llama-b9999-bin-win-cuda-12.4-x64.zip"
    assert cudart["name"] == "cudart-llama-bin-win-cuda-12.4-x64.zip"


def test_pick_release_assets_prefers_highest_cuda():
    assets = [_asset("llama-b9999-bin-win-cuda-11.7-x64.zip"),
              _asset("llama-b9999-bin-win-cuda-12.4-x64.zip")]
    main, cudart = llm.pick_release_assets(assets)
    assert "12.4" in main["name"] and cudart is None


def test_pick_release_assets_no_match_raises_with_names():
    with pytest.raises(llm.LLMConfigError) as exc:
        llm.pick_release_assets([_asset("llama-b9999-bin-macos-arm64.zip")])
    assert "macos-arm64" in str(exc.value)   # available names listed for debugging


# --- mb llm setup: net/zip helpers ------------------------------------------
import io
import zipfile as _zipfile


def _make_zip(tmp_path, entries, name="a.zip"):
    """entries: {archive_name: content_bytes}"""
    p = tmp_path / name
    with _zipfile.ZipFile(p, "w") as zf:
        for n, data in entries.items():
            zf.writestr(n, data)
    return p


def test_unpack_zip_flattens_single_top_dir(tmp_path):
    z = _make_zip(tmp_path, {"llama-b9999/llama-server.exe": b"exe",
                             "llama-b9999/lib/ggml.dll": b"dll"})
    target = tmp_path / "out"
    llm.unpack_zip(z, target)
    assert (target / "llama-server.exe").read_bytes() == b"exe"
    assert (target / "lib" / "ggml.dll").read_bytes() == b"dll"


def test_unpack_zip_root_files_kept_as_is(tmp_path):
    z = _make_zip(tmp_path, {"llama-server.exe": b"exe", "ggml.dll": b"dll"})
    target = tmp_path / "out"
    llm.unpack_zip(z, target)
    assert (target / "llama-server.exe").exists() and (target / "ggml.dll").exists()


def test_unpack_zip_rejects_traversal(tmp_path):
    z = _make_zip(tmp_path, {"../evil.txt": b"x"})
    with pytest.raises(llm.LLMConfigError):
        llm.unpack_zip(z, tmp_path / "out")


def test_unpack_zip_rejects_backslash_traversal(tmp_path):
    z = _make_zip(tmp_path, {"..\\evil.txt": b"x"})
    with pytest.raises(llm.LLMConfigError):
        llm.unpack_zip(z, tmp_path / "out")


def test_unpack_zip_rejects_windows_drive_absolute(tmp_path):
    z = _make_zip(tmp_path, {"C:\\Windows\\evil.dll": b"x"})
    with pytest.raises(llm.LLMConfigError):
        llm.unpack_zip(z, tmp_path / "out")


def test_ssl_context_returns_none_without_truststore(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "truststore":
            raise ImportError("no truststore")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert llm._ssl_context() is None


def test_fetch_json_parses_response(monkeypatch):
    import io

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda req, timeout=30, context=None: _Resp(b'{"a": 1}'))
    assert llm.fetch_json("https://x/y") == {"a": 1}


def test_download_writes_atomically_and_cleans_partial(tmp_path, monkeypatch):
    class _Resp(io.BytesIO):
        headers = {"Content-Length": "4"}
        def __enter__(self): return self
        def __exit__(self, *a): return False

    dest = tmp_path / "f.zip"
    monkeypatch.setattr(llm.urllib.request, "urlopen",
                        lambda *a, **k: _Resp(b"data"))
    llm.download("https://x/f.zip", dest)
    assert dest.read_bytes() == b"data"
    assert not dest.with_suffix(".zip.part").exists()

    def boom(*a, **k):
        raise OSError("net down")
    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(OSError):
        llm.download("https://x/g.zip", tmp_path / "g.zip")
    assert not (tmp_path / "g.zip.part").exists()   # partial cleaned


def test_server_bin_ok(tmp_path, monkeypatch):
    exe = tmp_path / "llama-server.exe"
    assert llm.server_bin_ok(exe) is False          # missing -> OSError -> False
    exe.write_text("x")
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    assert llm.server_bin_ok(exe) is True


# --- mb llm setup: orchestration --------------------------------------------

def _setup_env(tmp_path, monkeypatch, *, bin_ok=True, gguf_exists=True,
               claude=True, models=None):
    gguf = tmp_path / "m.gguf"
    if gguf_exists:
        gguf.write_text("x")
    if models is None:
        models = {"m1": {"gguf": str(gguf), "args": ["-fa", "--jinja"]}}
    exe = tmp_path / "bin" / "llama-server.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text("x")
    cfg = Config(vault=tmp_path, data={"llm": {
        "server_bin": str(exe), "server_host": "127.0.0.1",
        "server_port": 8080, "models": models,
        # keep setup()'s profile build off the real ~/.claude (home dir absent
        # → skills/agents mirror no-ops, CLAUDE.md is the minimal stub)
        "claude_home": str(tmp_path / "home"),
        "code_config_dir": str(tmp_path / "iso")}})
    monkeypatch.setattr(llm, "server_bin_ok", lambda p: bin_ok)
    monkeypatch.setattr(llm.shutil, "which",
                        lambda name: "/bin/claude" if claude else None)
    return cfg


def test_setup_healthy_env_all_ok_exit_0(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch)
    assert llm.setup(cfg) == 0
    out = capsys.readouterr().out
    assert "fixed" in out           # -fa repaired on first run
    # idempotent: second run fixes nothing
    assert llm.setup(cfg) == 0
    assert cfg.data["llm"]["models"]["m1"]["args"] == ["-fa", "on", "--jinja"]


def test_setup_creates_missing_llm_section(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(llm, "server_bin_ok", lambda p: True)
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/claude")
    monkeypatch.setattr(llm, "build_isolated_config", lambda *a, **k: None)  # section test, not profile
    assert llm.setup(cfg, dir_override=str(tmp_path / "inst")) == 0
    saved = json.loads(cfg.config_path.read_text(encoding="utf-8"))
    assert saved["llm"]["server_port"] == 8080
    assert str(tmp_path / "inst") in saved["llm"]["server_bin"]


def test_setup_builds_local_profile(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch)              # claude_home/code_config_dir → tmp
    assert llm.setup(cfg) == 0
    iso = tmp_path / "iso"
    assert (iso / "settings.json").exists()              # profile prebuilt by setup()
    assert (iso / "CLAUDE.md").read_text(encoding="utf-8") == llm.MINIMAL_CLAUDE_MD
    assert "local Claude profile ready" in capsys.readouterr().out


def test_setup_missing_gguf_warns_but_exit_0(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, gguf_exists=False)
    assert llm.setup(cfg) == 0
    assert "m.gguf" in capsys.readouterr().out    # warned with the path


def test_setup_downloads_when_binary_broken(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "win32")
    release = {"assets": [
        {"name": "llama-b9999-bin-win-cuda-12.4-x64.zip",
         "browser_download_url": "https://gh/main.zip"},
        {"name": "cudart-llama-bin-win-cuda-12.4-x64.zip",
         "browser_download_url": "https://gh/cudart.zip"}]}
    downloaded = []
    monkeypatch.setattr(llm, "fetch_json", lambda url, ctx=None: release)
    monkeypatch.setattr(llm, "download",
                        lambda url, dest, ctx=None: downloaded.append(url)
                        or dest.write_bytes(b"z"))
    unpacked = []
    monkeypatch.setattr(llm, "unpack_zip",
                        lambda z, t: unpacked.append(z.name))
    # after "unpack", the verify probe must succeed:
    monkeypatch.setattr(llm, "server_bin_ok",
                        lambda p: bool(unpacked))    # ok only post-unpack
    assert llm.setup(cfg) == 0
    assert downloaded == ["https://gh/main.zip", "https://gh/cudart.zip"]
    assert len(unpacked) == 2


def test_setup_handles_malformed_release_json(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "win32")

    def bad_fetch(url, ctx=None):
        raise json.JSONDecodeError("bad", "not json", 0)

    monkeypatch.setattr(llm, "fetch_json", bad_fetch)
    # should not raise -- caught and reported, not crash the process
    llm.setup(cfg)  # no assertion on return value needed beyond "did not raise"
    assert "fail" in capsys.readouterr().err.lower()


def test_setup_handles_corrupt_zip_download(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "win32")
    release = {"assets": [
        {"name": "llama-b9999-bin-win-cuda-12.4-x64.zip",
         "browser_download_url": "https://gh/main.zip"}]}
    monkeypatch.setattr(llm, "fetch_json", lambda url, ctx=None: release)
    monkeypatch.setattr(llm, "download", lambda url, dest, ctx=None: dest.write_bytes(b"not a zip"))

    def bad_unzip(z, t):
        raise zipfile.BadZipFile("File is not a zip file")

    monkeypatch.setattr(llm, "unpack_zip", bad_unzip)
    llm.setup(cfg)  # must not raise -- caught and reported
    assert "fail" in capsys.readouterr().err.lower()


def test_setup_handles_undecodable_release_response(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "win32")

    def bad_fetch(url, ctx=None):
        b"\xff\xfe".decode("utf-8")  # raises UnicodeDecodeError

    monkeypatch.setattr(llm, "fetch_json", bad_fetch)
    llm.setup(cfg)  # must not raise -- caught and reported
    assert "fail" in capsys.readouterr().err.lower()


def test_setup_posix_missing_binary_is_blocking(tmp_path, monkeypatch, capsys):
    cfg = _setup_env(tmp_path, monkeypatch, bin_ok=False)
    monkeypatch.setattr(llm.sys, "platform", "linux")
    assert llm.setup(cfg) == 1
    assert "manual" in capsys.readouterr().err.lower()


def test_cli_llm_setup_dispatches(tmp_path, monkeypatch):
    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg: cfg)
    seen = {}
    monkeypatch.setattr("singularity.llm.setup",
                        lambda c, dir_override=None: seen.update(d=dir_override) or 0)
    assert cli.main(["llm", "setup", "--dir", "X:/lc"]) == 0
    assert seen["d"] == "X:/lc"


# --- REV-3: start() lifecycle — intermediate state, interrupt cleanup, lock ---

def test_start_writes_starting_state_with_pid_before_health_check(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    seen = {}

    def _poll(*a, **k):
        # the PID must already be on disk while health is still being polled —
        # otherwise an interrupt right here would orphan an untracked process
        seen["state_during_poll"] = llm.load_state(cfg)
        return True

    monkeypatch.setattr(llm, "poll_health", _poll)
    assert llm.start(cfg, "qwen") == 0
    assert seen["state_during_poll"] == {
        "status": "starting", "model": "qwen", "server_pid": 4242,
        "server_port": 8080, "router_pid": None, "router_port": None,
        "started_at": seen["state_during_poll"]["started_at"],
    }
    assert llm.load_state(cfg)["status"] == "running"      # final state supersedes it


def test_start_interrupt_during_server_health_kills_pid_and_clears_state(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    killed = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm, "poll_health",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))

    assert llm.start(cfg, "qwen") == 130
    assert killed == [4242]
    assert llm.load_state(cfg) is None
    assert "interrupted" in capsys.readouterr().err
    assert not llm._start_lock_dir(cfg).exists()            # lock always released


def test_start_interrupt_during_router_health_kills_both_pids_and_clears_state(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path, with_router=True)
    killed = []
    order = []
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm.proc, "spawn_detached",
                        lambda argv, log: order.append(argv[0]) or (5000 + len(order)))

    calls = {"n": 0}
    def _poll(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return True                       # server healthy
        raise KeyboardInterrupt()              # interrupted while polling router

    monkeypatch.setattr(llm, "poll_health", _poll)
    assert llm.start(cfg, "qwen") == 130
    assert set(killed) == {5001, 5002}          # both this-run PIDs killed, none left orphaned
    assert llm.load_state(cfg) is None
    assert not llm._start_lock_dir(cfg).exists()


def test_start_crash_during_health_kills_pid_state_and_reraises(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    killed = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: killed.append(pid))
    monkeypatch.setattr(llm, "poll_health",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError, match="boom"):
        llm.start(cfg, "qwen")
    assert killed == [4242]
    assert llm.load_state(cfg) is None
    assert not llm._start_lock_dir(cfg).exists()


def test_start_refuses_when_lock_held_by_concurrent_start(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    spawned = []
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: spawned.append(argv) or 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)

    lock = llm._start_lock_dir(cfg)
    lock.mkdir(parents=True)                    # simulate a start already in progress

    assert llm.start(cfg, "qwen") == 1
    assert spawned == []                        # never even tried to spawn
    assert lock.exists()                        # the OTHER run's lock — must not be touched


def test_start_takes_over_stale_lock_and_still_releases_it(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 4242)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)

    lock = llm._start_lock_dir(cfg)
    lock.mkdir(parents=True)
    old = time.time() - 10_000                  # far older than any staleness window
    os.utime(lock, (old, old))

    assert llm.start(cfg, "qwen") == 0
    assert not lock.exists()                    # released after a successful takeover


def test_start_lock_released_on_ordinary_health_timeout(tmp_path, monkeypatch):
    cfg = _ready_llm(tmp_path)
    monkeypatch.setattr(llm.proc, "spawn_detached", lambda argv, log: 7777)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: False)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: None)
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: False)
    assert llm.start(cfg, "qwen") == 1
    assert not llm._start_lock_dir(cfg).exists()
