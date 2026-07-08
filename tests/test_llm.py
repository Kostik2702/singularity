"""Unit tests for mb llm — no real server/binary required."""
import http.server
import sys
import threading
from pathlib import Path

import pytest

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
