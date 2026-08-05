"""Gate for stale-daemon detection.

The failure this exists for produced numbers, not an error. `tools/search_eval.py`
reported a zero delta between hybrid on and hybrid off — on all three query
classes, to three decimals — because the warm daemon still served modules loaded
before the fusion branch existed. `mb daemon reload` had been run; it re-reads
the config and the indexes and never the code.

A measurement against the wrong build is worse than a crash: it is reproducible,
it looks like evidence, and recording it would have left "hybrid does not help"
in `docs/` permanently. So the tests below care mostly about the two ways this
check could quietly stop working: reporting stale when it cannot know, and
failing to report it when it can.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.machine.code_version import code_fingerprint, is_stale  # noqa: E402


def test_fingerprint_is_the_newest_source_mtime(tmp_path):
    old = tmp_path / "a.py"
    old.write_text("x = 1\n", encoding="utf-8")
    import os
    os.utime(old, ns=(1_000_000_000_000, 1_000_000_000_000))
    new = tmp_path / "b.py"
    new.write_text("y = 2\n", encoding="utf-8")
    os.utime(new, ns=(9_000_000_000_000, 9_000_000_000_000))

    assert code_fingerprint(tmp_path) == 9_000_000_000_000


def test_fingerprint_ignores_bytecode_caches(tmp_path):
    """`__pycache__` is rewritten by merely importing the package, so counting
    it would flag a daemon as stale on the next `mb status` after any run."""
    import os
    src = tmp_path / "a.py"
    src.write_text("x = 1\n", encoding="utf-8")
    os.utime(src, ns=(1_000_000_000_000, 1_000_000_000_000))
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    stale_pyc = cache / "a.cpython-313.py"
    stale_pyc.write_text("# not source\n", encoding="utf-8")
    os.utime(stale_pyc, ns=(9_000_000_000_000, 9_000_000_000_000))

    assert code_fingerprint(tmp_path) == 1_000_000_000_000


def test_fingerprint_of_an_unreadable_root_is_zero(tmp_path):
    assert code_fingerprint(tmp_path / "does-not-exist") == 0


def test_newer_source_than_the_serving_process_is_stale():
    assert is_stale(1_000, current=2_000) is True


def test_same_or_older_source_is_not_stale():
    assert is_stale(2_000, current=2_000) is False
    assert is_stale(3_000, current=2_000) is False


def test_unknown_on_either_side_is_never_reported_as_stale():
    """A daemon started before this check existed reports no fingerprint, and a
    root that cannot be read yields 0. Answering "stale" in either case would
    make the warning constant, and a constant warning is an ignored one — which
    is precisely how a real staleness would then slip through."""
    assert is_stale(None, current=2_000) is False
    assert is_stale(0, current=2_000) is False
    assert is_stale(1_000, current=0) is False


def test_the_real_package_fingerprints_to_something():
    """Guards against the check silently degrading to 0 for the actual engine —
    at which point `is_stale` would answer False forever and the whole gate
    would be decorative."""
    assert code_fingerprint() > 0


def test_status_reports_staleness_for_a_daemon_that_answers(monkeypatch, tmp_path):
    """The wiring, not the helper: `mb status` and `mb doctor` both read
    `code_stale` off this dict, and a ping that omits the field must degrade to
    'not stale' rather than raising."""
    from singularity.config import Config
    from singularity.search import client

    cfg = Config(vault=tmp_path, data={})
    monkeypatch.setattr(client, "_read_port", lambda c: 1234)
    monkeypatch.setattr(client, "_read_state", lambda c: {"instance_id": "i"})
    monkeypatch.setattr(client, "_request", lambda *a, **k: None)

    monkeypatch.setattr(client, "ping", lambda c: {"pid": 1, "code_fingerprint": 1})
    monkeypatch.setattr("singularity.machine.code_version.code_fingerprint",
                        lambda root=None: 999)
    assert client.status(cfg)["code_stale"] is True

    monkeypatch.setattr(client, "ping", lambda c: {"pid": 1})
    assert client.status(cfg)["code_stale"] is False
