"""Network layer for design-scout: honest UA, per-fetch timeout, budgets and
retry BY ERROR CLASS (CONTRACT.md §5). Raw exceptions never escape — every
failure maps to a typed class (§6). Injectable for offline tests."""
from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


def user_agent() -> str:
    from . import __version__
    return f"SingularityDesignScout/{__version__} (local design-reference tool)"


@dataclass
class FetchResult:
    ok: bool
    status: int | None = None
    body: bytes = b""
    error_class: str | None = None   # timeout|http-403|http-404|http-4xx|http-5xx|dns|ssl|other
    url: str = ""


@dataclass
class BudgetLog:
    """Run-scoped budget accounting, serialized into data.budget_log (§5)."""
    fetches: int = 0
    retries: int = 0
    thumb_downloads: int = 0
    started_at: float = field(default_factory=time.monotonic)
    stop_condition: str | None = None
    events: list = field(default_factory=list)

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def as_dict(self) -> dict:
        return {
            "fetches": self.fetches,
            "retries": self.retries,
            "thumb_downloads": self.thumb_downloads,
            "elapsed_s": round(self.elapsed(), 1),
            "stop_condition": self.stop_condition,
            "events": self.events,
        }


def classify(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 403:
            return "http-403"
        if exc.code == 404:
            return "http-404"
        if exc.code == 429:
            return "http-429"  # rate limit: retrying immediately only burns quota
        if 500 <= exc.code < 600:
            return "http-5xx"
        return "http-4xx"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "ssl"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "timeout"
        if isinstance(reason, ssl.SSLError):
            return "ssl"
        if isinstance(reason, socket.gaierror):
            return "dns"
        return "other"
    return "other"


MAX_RESPONSE_BYTES = 10_000_000  # §5 resource guard: bigger bodies -> too-large


def _urllib_transport(url: str, headers: dict, timeout: float) -> FetchResult:
    """Default network transport; every exception maps to a typed class."""
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return FetchResult(ok=False, status=resp.status,
                                   error_class="too-large", url=url)
            return FetchResult(ok=True, status=resp.status, body=body, url=url)
    except Exception as exc:  # noqa: BLE001 — mapped to typed class, never re-raised
        return FetchResult(ok=False, status=getattr(exc, "code", None),
                           error_class=classify(exc), url=url)


class Fetcher:
    """Budget + retry-by-class layer over an injectable transport. Tests pass
    a canned `transport(url, headers, timeout) -> FetchResult` and exercise
    the SAME retry/budget code paths the live tool runs (§5)."""

    def __init__(self, budgets: dict, log: BudgetLog, transport=None):
        self.timeout = float(budgets.get("per_fetch_timeout_s", 15))
        self.retry_max = int(budgets.get("retry_max", 1))
        self.retryable = set(budgets.get("retryable_classes", ["timeout", "http-5xx"]))
        self.wall_budget_s = float(budgets.get("wall_budget_s", 300))
        self.log = log
        self.transport = transport or _urllib_transport

    def __call__(self, url: str, headers: dict | None = None) -> FetchResult:
        if self.log.elapsed() >= self.wall_budget_s:  # 0 = no new fetches at all
            self.log.stop_condition = "wall_budget"
            return FetchResult(ok=False, error_class="budget-stop", url=url)
        merged = {"User-Agent": user_agent(), "Accept": "*/*"}
        merged.update(headers or {})
        attempts = 0
        while True:
            attempts += 1
            self.log.fetches += 1
            result = self.transport(url, merged, self.timeout)
            if result.ok:
                return result
            cls = result.error_class or "other"
            if cls in self.retryable and attempts <= self.retry_max:
                self.log.retries += 1
                self.log.events.append({"retry": url, "class": cls})
                continue
            self.log.events.append({"failed": url, "class": cls})
            return result
