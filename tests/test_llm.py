"""Unit tests for mb llm — no real server/binary required."""
import sys
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
