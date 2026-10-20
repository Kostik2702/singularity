"""`mb jev ...`: status from a journal, the key path, on/off, ask, pick, log."""
import io
import json

import pytest
from jev_server import KEY, FakeJev, answers_for, reply

from singularity import cli, config
from singularity.config import Config
from singularity.jev import client, log
from singularity.platform import secrets


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "singularity.json")
    (tmp_path / "vault" / ".mb").mkdir(parents=True)
    c = Config(vault=tmp_path / "vault", data={})
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: c)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv(client.KEY_ENV, raising=False)
    return c


@pytest.fixture
def stored(monkeypatch):
    """The OS keyring replaced by a dict - no test touches the real one."""
    store = {}
    monkeypatch.setattr(secrets, "set_secret",
                        lambda name, value, **kw: store.__setitem__(name, value) or "keyring")
    monkeypatch.setattr(secrets, "get_secret", lambda name: store.get(name))
    monkeypatch.setattr(secrets, "delete_secret", lambda name: store.pop(name, None) is not None)
    monkeypatch.setattr(secrets, "backend", lambda: "keyring")
    return store


def stdin(monkeypatch, text):
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def fixture_journal(cfg):
    log.append(cfg, "router", text="a", session="s1", size="tiny", confidence=0.9, decided="jev", cost=0.001)
    log.append(cfg, "router", text="b", session="s1", size="large", confidence=0.4, decided="self", cost=0.002)
    log.append(cfg, "router", text="c", session="s2", size="tiny", confidence=0.9, decided="jev", cost=0.004)
    log.append(cfg, "search", text="q", session="s2", error="http:401", cost=0.5)


def test_status_json_has_the_fields_the_tab_parses(cfg, stored, capsys, monkeypatch):
    fixture_journal(cfg)
    cfg.data.update({"jev_enabled": True, "jev_budget_usd": 1.0})
    stored[client.SECRET_NAME] = KEY
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s2")
    assert cli.main(["jev", "status", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert set(data) == {"enabled", "key_found", "key_backend", "model", "journal", "budget_usd",
                         "total_cost", "session_cost", "session", "balance_usd", "by_size",
                         "decided_self", "errors", "calls", "cost_by_kind"}
    assert data["enabled"] is True and data["key_found"] is True and data["key_backend"] == "keyring"
    assert data["model"] == "jev-1.13.0" and data["journal"] == str(log.path(cfg))
    assert data["by_size"] == {"tiny": 2, "small": 0, "medium": 0, "large": 1}
    assert data["decided_self"] == 1 and data["errors"] == 1 and data["calls"] == 4
    assert data["total_cost"] == pytest.approx(0.001 + 0.002 + 0.004)  # the failed call cost 0
    assert data["session"] == "s2" and data["session_cost"] == pytest.approx(0.004)
    assert data["balance_usd"] == pytest.approx(1.0 - 0.007)
    assert data["cost_by_kind"]["router"] == pytest.approx(0.007)
    assert KEY not in out


def test_status_without_budget_or_session(cfg, stored, capsys):
    assert cli.main(["jev", "status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["enabled"] is False and data["key_found"] is False and data["key_backend"] is None
    assert data["balance_usd"] is None and data["session_cost"] is None and data["calls"] == 0
    cli.main(["jev", "status"])
    text = capsys.readouterr().out
    assert "disabled" in text and "unknown (set jev_budget_usd)" in text


def test_key_set_reads_stdin_stores_under_the_fixed_name_and_never_echoes(cfg, stored, capsys, monkeypatch):
    stdin(monkeypatch, f"  {KEY}\n")
    assert cli.main(["jev", "key", "set"]) == 0
    captured = capsys.readouterr()
    assert stored == {"jev_api_key": KEY}
    assert captured.out.strip() == "stored in keyring"
    assert KEY not in captured.out + captured.err


@pytest.mark.parametrize("value", ["", "   \n", "two words", "key\nsecond-line"])
def test_key_set_refuses_empty_and_inner_whitespace(cfg, stored, capsys, monkeypatch, value):
    stdin(monkeypatch, value)
    assert cli.main(["jev", "key", "set"]) == 2
    assert stored == {}
    assert value.strip() == "" or value.split()[0] not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["jev", "key", "set", KEY],
    ["jev", "key", "set", "jev_api_key", KEY],
    ["jev", "key", KEY],
    ["jev", KEY],
])
def test_a_pasted_key_is_refused_and_never_echoed(cfg, stored, capsys, argv):
    try:
        rc = cli.main(argv)
    except SystemExit as exc:
        rc = exc.code
    captured = capsys.readouterr()
    assert rc == 2 and stored == {}
    assert KEY not in captured.out + captured.err


def test_key_set_refuses_a_terminal(cfg, stored, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr("sys.stdin", Tty(KEY))
    assert cli.main(["jev", "key", "set"]) == 2 and stored == {}


def test_key_status_and_delete(cfg, stored, capsys):
    assert cli.main(["jev", "key", "status"]) == 1
    assert "not found" in capsys.readouterr().out
    stored["jev_api_key"] = KEY
    assert cli.main(["jev", "key", "status"]) == 0
    out = capsys.readouterr().out
    assert "found" in out and KEY not in out
    assert cli.main(["jev", "key", "delete"]) == 0 and stored == {}


def test_on_and_off_toggle_the_flag_and_off_leaves_no_line(cfg, stored, capsys):
    assert cli.main(["jev", "on"]) == 0
    assert json.loads((config.CONFIG_FILE).read_text())["jev_enabled"] is True
    assert cli.main(["jev", "off"]) == 0
    assert "jev_enabled" not in json.loads((config.CONFIG_FILE).read_text())


def test_default_config_writes_no_jev_enabled_line(cfg):
    cfg.data["something"] = 1
    cfg.save()
    assert "jev_enabled" not in config.CONFIG_FILE.read_text()
    assert config.validate_config({"jev_enabled": "yes"})["errors"]
    assert config.validate_config({"jev_budget_usd": -1})["errors"]
    assert not config.validate_config({"jev_budget_usd": None, "jev_search": False})["errors"]


def test_ask_prints_answers_and_journals_a_line(cfg, stored, capsys, monkeypatch):
    cfg.data["jev_enabled"] = True
    srv = FakeJev([reply(200, answers_for({"q": {"type": "noul"}}))])
    try:
        monkeypatch.setenv(client.ENDPOINT_ENV, srv.url)
        monkeypatch.setenv(client.KEY_ENV, KEY)
        secret = "sk-ant-api03-" + "C" * 40
        stdin(monkeypatch, json.dumps({"state": f"hello {secret}",
                                       "questions": {"q": {"type": "noul", "instructions": "?"}}}))
        assert cli.main(["jev", "ask"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert set(out) == {"answers", "ms", "cost", "model", "usage"} and out["answers"]["q"]
        assert secret not in srv.sent_text()
        [row] = log.read(cfg)
        assert row["kind"] == "ask" and row["error"] is None
    finally:
        srv.close()


def test_ask_error_is_json_on_stdout_with_exit_1(cfg, stored, capsys, monkeypatch):
    cfg.data["jev_enabled"] = True
    srv = FakeJev([reply(422, {"detail": [{"msg": "Field required"}]})])
    try:
        monkeypatch.setenv(client.ENDPOINT_ENV, srv.url)
        monkeypatch.setenv(client.KEY_ENV, KEY)
        stdin(monkeypatch, json.dumps({"state": "s", "questions": {"q": {"type": "score"}}}))
        assert cli.main(["jev", "ask"]) == 1
        err = json.loads(capsys.readouterr().out)["error"]
        assert err["kind"] == "http" and err["status"] == 422 and "Field required" in err["body"]
        assert log.read(cfg)[0]["error"] == "http:422"
    finally:
        srv.close()


def test_ask_refuses_when_jev_is_off_and_on_bad_stdin(cfg, stored, capsys, monkeypatch):
    stdin(monkeypatch, "{}")
    assert cli.main(["jev", "ask"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "off"
    cfg.data["jev_enabled"] = True
    stdin(monkeypatch, "not json")
    assert cli.main(["jev", "ask"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["kind"] == "bad_json"


@pytest.mark.parametrize("enabled,key,expect", [(False, True, "jev off"), (True, False, "no key")])
def test_pick_says_why_it_cannot_decide_and_exits_zero(cfg, stored, capsys, monkeypatch, enabled, key, expect):
    if enabled:
        cfg.data["jev_enabled"] = True
    if key:
        stored["jev_api_key"] = KEY
    assert cli.main(["jev", "pick", "review", "the", "diff"]) == 0
    assert capsys.readouterr().out.strip() == f"pick: decide yourself ({expect})"


def test_pick_answers_and_respects_the_threshold(cfg, stored, capsys, monkeypatch):
    cfg.data["jev_enabled"] = True
    answer = lambda c: lambda req: answers_for(  # noqa: E731
        req["questions"], tier={"type": "choice", "choice": "opus·high", "confidence": c,
                                "probabilities": {}})
    srv = FakeJev([reply(200, answer(0.74)), reply(200, answer(0.41))])
    try:
        monkeypatch.setenv(client.ENDPOINT_ENV, srv.url)
        monkeypatch.setenv(client.KEY_ENV, KEY)
        assert cli.main(["jev", "pick", "review this"]) == 0
        assert capsys.readouterr().out.strip() == "pick: opus·high (conf 0.74)"
        assert cli.main(["jev", "pick", "review this"]) == 0
        assert capsys.readouterr().out.strip() == "pick: decide yourself (conf 0.41)"
        options = list(srv.requests[0]["body"]["questions"]["tier"]["criteria"])
        assert options == ["haiku·low", "sonnet·medium", "sonnet·high", "opus·high"]
        assert [r["decided"] for r in log.read(cfg)] == ["jev", "self"]
    finally:
        srv.close()


def test_pick_with_an_unreachable_server_decides_yourself(cfg, stored, capsys, monkeypatch):
    cfg.data["jev_enabled"] = True
    monkeypatch.setenv(client.KEY_ENV, KEY)
    monkeypatch.setenv(client.ENDPOINT_ENV, "http://127.0.0.1:9/x")
    assert cli.main(["jev", "pick", "x"]) == 0
    assert capsys.readouterr().out.startswith("pick: decide yourself (error ")


def test_log_tails_and_filters_by_session(cfg, stored, capsys):
    fixture_journal(cfg)
    assert cli.main(["jev", "log", "--tail", "2"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    assert cli.main(["jev", "log", "--session", "s1"]) == 0
    assert {json.loads(x)["session"] for x in capsys.readouterr().out.splitlines()} == {"s1"}


def test_the_journal_rotates_past_5_mib(cfg):
    target = log.path(cfg)
    target.parent.mkdir(parents=True)
    target.write_text("x" * (log.MAX_BYTES + 1))
    assert log.append(cfg, "router", text="after")
    assert (target.parent / "log.1.jsonl").exists()
    assert len(log.read(cfg)) == 1


def test_the_journal_head_is_short_flat_and_redacted(cfg):
    secret = "sk-ant-api03-" + "D" * 40
    log.append(cfg, "router", text=f"line one\nline two {secret} " + "w" * 200)
    row = log.read(cfg)[0]
    assert len(row["head"]) <= 80 and "\n" not in row["head"] and secret not in json.dumps(row)
    assert row["chars"] > 200


def test_status_text_shows_costs_at_six_decimals(cfg, stored, capsys):
    log.append(cfg, "router", text="a", size="tiny", cost=0.0000231)
    cfg.data["jev_budget_usd"] = 1.0
    cli.main(["jev", "status"])
    out = capsys.readouterr().out
    assert "$0.000023 all time" in out and "$0.999977" in out


def test_pick_never_raises(cfg, stored, capsys, monkeypatch):
    cfg.data["jev_enabled"] = True
    stored["jev_api_key"] = KEY

    def boom(*a, **k):
        raise RuntimeError("anything")

    monkeypatch.setattr(client, "ask", boom)
    assert cli.main(["jev", "pick", "x"]) == 0
    assert capsys.readouterr().out.strip() == "pick: decide yourself (error internal)"
    assert log.read(cfg)[0]["error"] == "internal:null"


def test_stats_read_both_generations(cfg):
    log.append(cfg, "router", text="old", size="tiny", cost=1.0)
    target = log.path(cfg)
    target.replace(target.with_name("log.1.jsonl"))
    log.append(cfg, "router", text="new", size="small", cost=2.0)
    stats = log.stats(cfg)
    assert stats["calls"] == 2 and stats["cost_total"] == pytest.approx(3.0)
    assert [r["head"] for r in log.read(cfg)] == ["old", "new"]


def test_the_all_time_cost_survives_two_rotations(cfg, monkeypatch):
    monkeypatch.setattr(log, "MAX_BYTES", 600)
    for i in range(30):
        log.append(cfg, "router", text=f"call {i}", size="tiny", cost=1.0)
    stats = log.stats(cfg)
    assert stats["cost_total"] == pytest.approx(30.0)
    assert stats["calls"] < 30                       # old lines really are gone
    assert log.carried_cost(cfg) > 0


def test_two_writers_rotating_together_lose_no_cost(cfg, monkeypatch):
    import threading

    monkeypatch.setattr(log, "MAX_BYTES", 4000)

    def writer():
        for _ in range(150):
            log.append(cfg, "router", text="x", size="tiny", cost=1.0)

    threads = [threading.Thread(target=writer) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert log.stats(cfg)["cost_total"] == pytest.approx(600.0)
    assert not (log.jev_dir(cfg) / "rotate.lock").exists()
