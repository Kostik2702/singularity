"""The daemon's counters: shape of the service, never its content (AUD-18).

`mb status` used to say the daemon was up and nothing about whether it was
answering well. These counters close that, and the two properties worth pinning
are the ones that make them safe to have on by default: they record no query
text, and they describe only the running process.
"""
from __future__ import annotations

from singularity.search.metrics import Counters


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_counts_and_uptime():
    clock = _FakeClock()
    m = Counters(clock=clock)
    m.incr("searches")
    m.incr("searches")
    m.incr("errors")
    clock.t += 12.5
    snap = m.snapshot()
    assert snap["searches"] == 2 and snap["errors"] == 1
    assert snap["uptime_s"] == 12.5


def test_latency_reports_the_tail_not_an_average():
    """An average hides the tail, and the tail is what a person notices."""
    m = Counters()
    for ms in [10.0] * 19 + [900.0]:
        m.observe("search", ms)
    stats = m.snapshot()["search_ms"]
    assert stats["n"] == 20
    assert stats["p50"] == 10.0
    assert stats["max"] == 900.0
    assert stats["p95"] >= 10.0


def test_percentiles_only_report_measured_values():
    m = Counters()
    for ms in (5.0, 25.0, 100.0):
        m.observe("search", ms)
    stats = m.snapshot()["search_ms"]
    assert stats["p50"] in (5.0, 25.0, 100.0)
    assert stats["p95"] in (5.0, 25.0, 100.0)


def test_samples_are_bounded_so_a_long_lived_daemon_cannot_grow():
    m = Counters()
    for i in range(1000):
        m.observe("search", float(i))
    assert m.snapshot()["search_ms"]["n"] == 256


def test_a_snapshot_carries_no_content():
    """The line that lets these run by default: counts and durations only."""
    m = Counters()
    m.incr("searches")
    m.observe("search", 12.0)
    snap = m.snapshot()
    flat = repr(snap)
    for forbidden in ("query", "vault", "/", "\\\\", "text"):
        assert forbidden not in flat, f"a snapshot leaked {forbidden!r}: {flat}"


def test_unobserved_metrics_are_simply_absent():
    assert "search_ms" not in Counters().snapshot()
