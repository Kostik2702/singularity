"""The Jev client against a local server: parsing, cost, errors, retries, deadline."""
import json
import time

import pytest
from jev_server import KEY, FakeJev, answers_for, reply

from singularity.jev import client

Q = {"q": {"type": "noul", "instructions": "?", "criteria": {"true": "a", "false": "b"}}}


@pytest.fixture
def server():
    made = []

    def make(*script):
        srv = FakeJev(script)
        made.append(srv)
        return srv

    yield make
    for srv in made:
        srv.close()


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(client, "RETRY_PAUSES", (0.05, 0.05, 0.05, 0.05, 0.05))


def test_200_parses_and_prices_the_input_tokens(server):
    body = answers_for(Q, q={"type": "noul", "noul": 0.89})
    srv = server(reply(200, body, {"x-typesafe-request-id": "req_abc"}))
    out = client.ask("state", Q, key=KEY, endpoint=srv.url)
    assert out["answers"]["q"]["noul"] == 0.89
    assert out["cost"] == pytest.approx(1000 * 0.042 / 1_000_000)
    assert out["request_id"] == "req_abc"
    assert out["usage"]["output_tokens"] == 10
    sent = srv.requests[0]
    assert sent["auth"] == f"Bearer {KEY}"
    assert sent["body"]["model"] == "jev-1.13.0"
    assert sent["body"]["questions"] == Q


def test_no_request_id_header_is_none(server):
    srv = server(reply(200, answers_for(Q)))
    assert client.ask("s", Q, key=KEY, endpoint=srv.url)["request_id"] is None


@pytest.mark.parametrize("status,body", [
    (401, {"detail": {"error_type": "authentication_error",
                      "message": "Cannot authenticate with the server."}}),
    (422, {"detail": [{"type": "missing", "loc": ["body", "questions", "q", "score", "criteria"],
                       "msg": "Field required"}]}),
])
def test_http_errors_carry_status_and_body_but_never_the_key(server, status, body):
    # the server echoes the key back, the worst case for a leak
    body = {**body, "echo": KEY}
    srv = server(reply(status, body))
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert err.value.kind == "http" and err.value.status == status
    assert str(status) in str(err.value)
    assert ("authentication_error" in err.value.body) or ("Field required" in err.value.body)
    assert KEY not in str(err.value) and KEY not in err.value.body


def test_429_then_200_is_retried(server):
    srv = server(reply(429, {"detail": "slow down"}), reply(200, answers_for(Q)))
    out = client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert "q" in out["answers"] and len(srv.requests) == 2


def test_other_statuses_are_not_retried(server):
    srv = server(reply(500, {"detail": "boom"}), reply(200, answers_for(Q)))
    with pytest.raises(client.JevError):
        client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert len(srv.requests) == 1


def test_retries_stop_after_five(server):
    srv = server(reply(529, {"detail": "overloaded"}))
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert err.value.status == 529 and len(srv.requests) == 6


def test_the_deadline_stops_retries(server, monkeypatch):
    monkeypatch.setattr(client, "RETRY_PAUSES", (5, 5, 5, 5, 5))
    srv = server(reply(429, {"detail": "slow"}), reply(200, answers_for(Q)))
    t0 = time.monotonic()
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=srv.url, deadline=time.monotonic() + 1.0)
    assert time.monotonic() - t0 < 2.5
    assert err.value.status == 429 and len(srv.requests) == 1


def test_retry_after_is_honoured(server):
    srv = server(reply(429, {}, {"retry-after": "0.3"}), reply(200, answers_for(Q)))
    t0 = time.monotonic()
    client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert time.monotonic() - t0 >= 0.3


def test_a_slow_server_is_a_timeout(server):
    srv = server(reply(200, answers_for(Q), delay=2.0))
    t0 = time.monotonic()
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=srv.url, timeout=0.3)
    assert err.value.kind == "timeout" and time.monotonic() - t0 < 1.5


def test_malformed_json_is_bad_json(server):
    srv = server(reply(200, "{not json"))
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert err.value.kind == "bad_json"


def test_an_answer_missing_for_a_question_is_bad_json(server):
    srv = server(reply(200, {"model": "jev-1.13.0", "answers": {}, "usage": {}}))
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=srv.url)
    assert err.value.kind == "bad_json"


def test_no_key_never_touches_the_network(server, monkeypatch):
    monkeypatch.setattr(client, "resolve_key", lambda: None)
    srv = server(reply(200, answers_for(Q)))
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, endpoint=srv.url)
    assert err.value.kind == "no_key" and not srv.requests


def test_refused_connection_is_network(monkeypatch):
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint="http://127.0.0.1:9/v1/systemone", timeout=1)
    assert err.value.kind in ("network", "timeout")


def test_env_key_overrides_the_stored_one(monkeypatch):
    monkeypatch.setenv(client.KEY_ENV, "  from-env  ")
    assert client.resolve_key() == "from-env"


# --- failures that are not OSError ---------------------------------------------------

@pytest.mark.parametrize("payload", [
    b"this is not http at all\r\n\r\n",
    b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{\"answers\": {",     # closed mid-body
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\nzz\r\n",     # bad chunk size
    b"",                                                                   # closed with no reply
])
def test_every_transport_failure_is_a_jev_error(payload):
    from jev_server import RawServer

    srv = RawServer(payload)
    try:
        with pytest.raises(client.JevError) as err:
            client.ask("s", Q, key=KEY, endpoint=srv.url, timeout=3)
        assert err.value.kind in ("network", "bad_json", "timeout")
        assert KEY not in str(err.value)
    finally:
        srv.close()


def test_a_redirect_is_an_error_and_the_key_goes_nowhere_else(server):
    elsewhere = server(reply(200, answers_for(Q)))
    first = server(reply(302, {}, {"Location": elsewhere.url}))
    with pytest.raises(client.JevError) as err:
        client.ask("s", Q, key=KEY, endpoint=first.url)
    assert err.value.kind == "http" and err.value.status == 302
    assert not elsewhere.requests


def test_the_deadline_is_total_even_for_a_slow_drip():
    from jev_server import RawServer

    ok = json.dumps(answers_for(Q))
    payload = (f"HTTP/1.1 200 OK\r\nContent-Length: {len(ok)}\r\n\r\n{ok}").encode()
    srv = RawServer(payload, drip=0.2)       # ~20 s to finish, one byte at a time
    try:
        t0 = time.monotonic()
        with pytest.raises(client.JevError) as err:
            client.ask("s", Q, key=KEY, endpoint=srv.url, timeout=5,
                       deadline=time.monotonic() + 1.0)
        assert err.value.kind == "timeout" and time.monotonic() - t0 < 2.5
    finally:
        srv.close()


def test_the_endpoint_override_needs_the_env_key_too(monkeypatch):
    monkeypatch.setenv(client.ENDPOINT_ENV, "http://127.0.0.1:1/x")
    monkeypatch.delenv(client.KEY_ENV, raising=False)
    assert client._endpoint("stored-key", None) == client.ENDPOINT      # ignored
    monkeypatch.setenv(client.KEY_ENV, "env-key")
    assert client._endpoint("stored-key", None) == client.ENDPOINT      # a different key
    assert client._endpoint("env-key", None) == "http://127.0.0.1:1/x"


@pytest.mark.parametrize("url", ["http://example.com/v1", "ftp://127.0.0.1/x", "http://127.0.0.1.evil.net/x"])
def test_plain_http_is_for_this_machine_only(url):
    with pytest.raises(client.JevError):
        client._endpoint(KEY, url)
    assert client._endpoint(KEY, "http://localhost:8080/x")
    assert client._endpoint(KEY, "https://api.typesafe.ai/v1/systemone")


def test_the_error_tag_spells_a_missing_status_as_null():
    assert client.JevError("timeout").tag == "timeout:null"
    assert client.JevError("http", status=401).tag == "http:401"
