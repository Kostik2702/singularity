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
    calls = {"kill": [], "spawn": 0}
    monkeypatch.setattr(llm.proc, "is_running", lambda pid: pid == 999)
    monkeypatch.setattr(llm.proc, "kill", lambda pid: calls["kill"].append(pid))
    def _spawn(argv, log):
        calls["spawn"] += 1
        return 4242
    monkeypatch.setattr(llm.proc, "spawn_detached", _spawn)
    monkeypatch.setattr(llm, "poll_health", lambda *a, **k: True)
    assert llm.start(cfg, "qwen") == 0
    assert 999 in calls["kill"]                     # old server killed before new spawn
    assert llm.load_state(cfg)["server_pid"] == 4242


def test_start_missing_gguf_returns_1(tmp_path, monkeypatch, capsys):
    cfg = _ready_llm(tmp_path)
    Path(cfg.get("llm")["models"]["qwen"]["gguf"]).unlink()   # remove gguf
    assert llm.start(cfg, "qwen") == 1
    assert "gguf not found" in capsys.readouterr().err
