"""Counters the daemon keeps about itself (AUD-18).

`mb status` could say the daemon was up and nothing about whether it was
answering well: a search that took two seconds, a reload that failed, a client
that fell back to a cold one-off because the socket was busy — all invisible
unless someone was watching the log at the time.

Two deliberate limits:

* **Nothing about content.** Queries, results and paths never enter a counter.
  What is recorded is how many, how long, and how often it broke — the shape of
  the service, not what anyone searched for. That is the same line
  `mb reflect` draws, and it is the reason these can be on by default.
* **Memory only, no persistence.** They describe THIS process since it started
  (`uptime_s` says since when), so a restart resets them and no file has to be
  written, locked or rotated. A number that survives a restart would need a
  retention policy to stay honest.

Latency is kept as a bounded ring of recent samples rather than a running mean:
an average hides the tail, and the tail is what a person notices.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

SAMPLE_LIMIT = 256


class Counters:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._started = clock()
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}
        self._latency: dict[str, deque[float]] = {}

    def incr(self, name: str, by: int = 1) -> None:
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + by

    def observe(self, name: str, ms: float) -> None:
        with self._lock:
            samples = self._latency.setdefault(name, deque(maxlen=SAMPLE_LIMIT))
            samples.append(float(ms))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counts = dict(self._counts)
            latency = {name: sorted(s) for name, s in self._latency.items() if s}
            uptime = self._clock() - self._started
        out: dict[str, Any] = {"uptime_s": round(uptime, 1), **counts}
        for name, samples in latency.items():
            out[f"{name}_ms"] = {
                "n": len(samples),
                "p50": round(_percentile(samples, 0.50), 1),
                "p95": round(_percentile(samples, 0.95), 1),
                "max": round(samples[-1], 1),
            }
        return out


def _percentile(sorted_samples: list[float], q: float) -> float:
    """Nearest-rank percentile — exact for the small windows kept here, and it
    never invents a value that was not measured."""
    if not sorted_samples:
        return 0.0
    idx = max(0, min(len(sorted_samples) - 1, int(round(q * len(sorted_samples))) - 1))
    return sorted_samples[idx]
