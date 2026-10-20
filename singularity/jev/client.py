"""The one place that talks HTTP to Jev (TypeSafe `/v1/systemone`).

stdlib only: this runs inside a UserPromptSubmit hook, where an import floor
and a hard time budget matter more than a nicer API. The key never leaves this
module except in the Authorization header - it is not in argv, in an error
message, or in a log line (every body that reaches an error is scrubbed).

Three guarantees the callers lean on, each with a test:
* every failure is a `JevError` - a half-open socket, a garbage status line, a
  TLS error all arrive as one type, so no caller needs a net of its own;
* the deadline is TOTAL: the request runs in a daemon thread that is abandoned
  at the deadline, because a socket timeout bounds one read, not a slow drip or
  a resolver that never answers;
* the key goes only to the vendor (or to a local server, with the key from the
  environment too), and never follows a redirect.

Retries are for 429 and 529 only, on the vendor's pauses, and never past the
caller's deadline: a hook with a 5 s budget must not sleep through 16 s.
"""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PRICE_PER_INPUT_TOKEN_USD = 0.042 / 1_000_000   # models page: input only, output is free
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"        # pinned: the thresholds are tuned against one version
SECRET_NAME = "jev_api_key"
KEY_ENV = "MB_JEV_API_KEY"
ENDPOINT_ENV = "MB_JEV_ENDPOINT"    # a test seam: honoured only together with KEY_ENV
REQUEST_ID_HEADER = "x-typesafe-request-id"
RETRY_PAUSES = (1, 2, 4, 8, 16)
RETRY_STATUSES = (429, 529)
MAX_RETRY_AFTER = 60.0
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_BODY_CAP = 2000


class JevError(Exception):
    """kind is one of no_key, http, timeout, network, bad_json."""

    def __init__(self, kind: str, message: str = "", *, status: int | None = None,
                 body: str = ""):
        self.kind = kind
        self.status = status
        self.body = body[:_BODY_CAP]
        text = f"jev {kind}" + (f" {status}" if status is not None else "")
        if message:
            text += f": {message}"
        if self.body:
            text += f": {self.body}"
        super().__init__(text)

    @property
    def tag(self) -> str:
        """The journal's `error` spelling: `<kind>:<status>`, `null` without one."""
        return f"{self.kind}:{self.status if self.status is not None else 'null'}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib re-sends a redirected request with its headers - the Bearer key
    included - to whatever host the answer names. A 3xx is an error here."""

    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def resolve_key() -> str | None:
    """The env override wins and the stored key is then never touched."""
    env = (os.environ.get(KEY_ENV) or "").strip()
    if env:
        return env
    try:
        from ..platform import secrets

        return (secrets.get_secret(SECRET_NAME) or "").strip() or None
    except Exception:  # noqa: BLE001 - a dead keyring is "no key", not a crash
        return None


def _scrub(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def _endpoint(key: str, explicit: str | None) -> str:
    """The URL this key may be sent to. The override is a test seam: it applies
    only when the key itself comes from the environment, so a stored key never
    follows a stray variable to another host. Plain http only for this machine."""
    url = explicit
    if not url:
        override = os.environ.get(ENDPOINT_ENV)
        if override and (os.environ.get(KEY_ENV) or "").strip() == key:
            url = override
    url = url or ENDPOINT
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOCAL_HOSTS):
        return url
    raise JevError("network", "refusing an endpoint that is neither https nor local")


def _retry_pause(attempt: int, headers) -> float:
    pause = float(RETRY_PAUSES[attempt])
    raw = headers.get("retry-after") if headers is not None else None
    if raw:
        try:
            pause = min(max(float(raw), 0.0), MAX_RETRY_AFTER)
        except ValueError:
            pass
    return pause


def _post(url: str, body: bytes, key: str, timeout: float) -> tuple[int, str, object]:
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace"), resp.headers
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except (OSError, ValueError, http.client.HTTPException):
            text = ""
        return exc.code, text, exc.headers


def _post_bounded(url: str, body: bytes, key: str, budget: float) -> tuple[int, str, object]:
    """`_post`, abandoned at `budget` seconds wall-clock however it is stalling."""
    box: dict = {}

    def run() -> None:
        try:
            box["ok"] = _post(url, body, key, budget)
        except BaseException as exc:  # noqa: BLE001 - re-raised below, in the caller
            box["err"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(budget)
    if worker.is_alive():
        raise JevError("timeout", f"no answer within {budget:.1f}s")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def ask(state, questions: dict, *, timeout: float = 10.0, deadline: float | None = None,
        key: str | None = None, endpoint: str | None = None) -> dict:
    """One request, every question in it answered in parallel by Jev.

    `deadline` is a `time.monotonic()` instant. Returns
    `{"answers", "model", "usage", "cost", "ms", "request_id"}` or raises
    `JevError` - nothing else."""
    key = key or resolve_key()
    if not key:
        raise JevError("no_key", "no API key (mb jev key set)")
    url = _endpoint(key, endpoint)
    try:
        body = json.dumps({"model": MODEL, "state": state, "questions": questions},
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise JevError("bad_json", f"request is not serialisable: {exc}") from exc

    started = time.monotonic()
    attempt = 0
    while True:
        budget = timeout
        if deadline is not None:
            budget = min(timeout, deadline - time.monotonic())
            if budget <= 0:
                raise JevError("timeout", "deadline reached")
        try:
            status, text, headers = _post_bounded(url, body, key, budget)
        except JevError:
            raise
        except TimeoutError as exc:
            raise JevError("timeout", f"no answer within {budget:.1f}s") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise JevError("timeout", f"no answer within {budget:.1f}s") from exc
            raise JevError("network", _scrub(str(exc.reason), key)) from exc
        except Exception as exc:  # noqa: BLE001 - HTTPException, ssl, OSError, ValueError, ...
            raise JevError("network", _scrub(f"{type(exc).__name__}: {exc}", key)) from exc

        if status in RETRY_STATUSES and attempt < len(RETRY_PAUSES):
            pause = _retry_pause(attempt, headers)
            if deadline is None or time.monotonic() + pause < deadline:
                time.sleep(pause)
                attempt += 1
                continue
        if status != 200:
            raise JevError("http", status=status, body=_scrub(text, key))
        break

    try:
        data = json.loads(text)
    except ValueError as exc:
        raise JevError("bad_json", "response is not JSON", body=_scrub(text, key)) from exc
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict) or any(q not in answers for q in questions):
        raise JevError("bad_json", "response lacks an answer for every question",
                       body=_scrub(text, key))
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    tokens = usage.get("input_tokens")
    cost = tokens * PRICE_PER_INPUT_TOKEN_USD if isinstance(tokens, (int, float)) else 0.0
    request_id = headers.get(REQUEST_ID_HEADER) if headers is not None else None
    return {"answers": answers, "model": data.get("model") or MODEL, "usage": usage,
            "cost": cost, "ms": int((time.monotonic() - started) * 1000),
            "request_id": request_id}
