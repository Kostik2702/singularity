"""Shared test isolation from the developer's REAL machine.

Two incidents motivated this file, both of the same shape — a test reached a
side-effecting installer and mutated the machine it was running on:

* 2026-07-23: `doctor.run(fix=True)` ran the real Gemini/Grok registrars and
  rewrote `~/.gemini` / `~/.grok`. Fixed at the time by `test_doctor.py::
  _stub_externals` — a PRIVATE helper inside one file.
* 2026-07-28: a new test file called `doctor.run(fix=True)` without knowing
  that helper existed, and re-registered the real `Singularity-nightly` /
  `Singularity-watch` scheduled tasks against a pytest temp vault. The
  developer's watcher and nightly job pointed at
  `...\\pytest-of-Admin\\pytest-368\\...` until repaired by hand.

The lesson from the second one is not "be careful" — it is that the isolation
lived somewhere unreachable, so every new test file had to rediscover the
requirement. Hence:

* `_no_real_scheduler` (autouse) makes the mistake FAIL LOUDLY instead of
  silently mutating the machine. Tests that legitimately drive the scheduler
  opt out with `@pytest.mark.real_scheduler`; tests that want to observe
  registration keep stubbing `register`/`unregister` themselves (their
  monkeypatch runs after this fixture and wins).
* `isolated_machine` is the one obvious thing a new test should request when
  it calls anything with `fix=True`.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from singularity.platform import scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_scheduler(request, monkeypatch):
    """Fail loudly instead of registering a real OS scheduled task.

    Guards the PUBLIC entry points, which is where application code
    (`doctor`, `nightly`, `cli.cmd_uninstall`, the installers) calls in. The
    private per-OS implementations stay reachable for the tests that verify
    their argv/plist serialization directly."""
    if request.node.get_closest_marker("real_scheduler"):
        return

    def _blocked(job, *_args, **_kwargs):
        raise AssertionError(
            f"test tried to {'un' if _blocked.op == 'unregister' else ''}register the "
            f"REAL OS scheduled task {getattr(job, 'task_id', job)!r}. Request the "
            f"`isolated_machine` fixture, stub singularity.platform.scheduler "
            f"yourself, or mark the test @pytest.mark.real_scheduler if it genuinely "
            f"drives the scheduler backend.")

    def _blocked_register(job, *a, **k):
        _blocked.op = "register"
        return _blocked(job, *a, **k)

    def _blocked_unregister(job, *a, **k):
        _blocked.op = "unregister"
        return _blocked(job, *a, **k)

    monkeypatch.setattr(scheduler, "register", _blocked_register)
    monkeypatch.setattr(scheduler, "unregister", _blocked_unregister)


@pytest.fixture(autouse=True)
def _no_real_user_path(request, monkeypatch):
    """Fail loudly instead of rewriting the developer's real user PATH.

    One machine-mutating surface later (issue #15): the PATH registrar and
    `mb uninstall` write `HKCU\\Environment` on Windows. The POSIX half of that
    feature symlinks under `Path.home()`, which tests already fake — the
    registry has no such indirection, so it is blocked at `winreg` itself,
    which is the one module object every importer shares (`register_path` is
    loaded fresh by path each time, so patching that module would not hold).

    Two layers, because either one alone is insufficient. The write is blocked
    so a test that tries it fails loudly instead of silently editing the
    developer's environment. The read is answered with an EMPTY PATH so the
    common case — `mb uninstall`, which removes only entries it finds — has
    nothing to find and never reaches the write at all; otherwise the verdict
    would differ between a machine that has run the installer and one that has
    not (caught exactly this way by test_wiki_loop_e2e)."""
    if request.node.get_closest_marker("real_user_path"):
        return
    try:
        import winreg
    except ImportError:
        return                      # not Windows: nothing to protect

    def _blocked(*_args, **_kwargs):
        raise AssertionError(
            "test tried to WRITE the real Windows user environment (PATH). "
            "Stub install/register_path.py, or mark the test "
            "@pytest.mark.real_user_path if it genuinely drives the registry.")

    monkeypatch.setattr(winreg, "SetValueEx", _blocked)
    monkeypatch.setattr(winreg, "QueryValueEx",
                        lambda key, name: ("", winreg.REG_EXPAND_SZ))


@pytest.fixture(autouse=True)
def _no_real_machine_config(request, tmp_path_factory, monkeypatch):
    """Never let a test read or write the developer's real machine config.

    From 0.7 the config lives at `~/.singularity.json` instead of inside the
    vault, and `load_config()` / `cfg.save()` / `write_pointer()` all write it.
    A vault fixture is a tmp_path, but `CONFIG_FILE` is an absolute path in
    `$HOME` that no tmp vault redirects — so without this every test that loads
    a config would rewrite the machine's real one, and the seamless migration
    would fire against the developer's actual vault.

    Third guard of the same family as `_no_real_scheduler` and
    `_no_real_user_path`. Opt out with `@pytest.mark.real_machine_config`."""
    if request.node.get_closest_marker("real_machine_config"):
        return
    from singularity import config as config_mod

    fake = tmp_path_factory.mktemp("machine-config") / ".singularity.json"
    monkeypatch.setattr(config_mod, "CONFIG_FILE", fake)
    monkeypatch.setattr(config_mod, "POINTER_FILE", fake)


@pytest.fixture(autouse=True)
def _no_real_version_managers(request, monkeypatch):
    """A test that says "this CLI is not installed" must stay right.

    Issue #10 widened CLI discovery past `PATH` into `~/.nvm`, `~/.volta`,
    `~/.fnm`, `/opt/homebrew/bin`, `/usr/local/bin` and friends — none of which
    a fake `Path.home()` or a stubbed `shutil.which` redirects. On a developer
    machine that really does have `gemini` under nvm, the Gemini/Grok tests
    would otherwise find it and assert against the wrong world.

    Opt out with `@pytest.mark.real_version_managers`."""
    if request.node.get_closest_marker("real_version_managers"):
        return
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "install"))
    import cli_locator

    monkeypatch.setattr(cli_locator, "search_locations", lambda: [])


@pytest.fixture
def isolated_machine(tmp_path, monkeypatch):
    """Neutralize every outward-facing effect of a `fix=True` code path.

    Returns the fake home so a test can assert against what WOULD have been
    written. Covers: `Path.home()` (client profiles, skill links, launch
    agents), `$GROK_HOME` (its own resolver, a leaked env var would point at a
    real Grok home), the scheduler, and the search daemon."""
    from singularity.search import client

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("GROK_HOME", raising=False)

    monkeypatch.setattr(scheduler, "register", lambda job: "OK")
    monkeypatch.setattr(scheduler, "unregister", lambda job: None)
    monkeypatch.setattr(scheduler, "verify",
                        lambda job: {"registered": True, "action_ok": True})

    monkeypatch.setattr(client, "ping", lambda cfg: None)
    monkeypatch.setattr(client, "start_daemon", lambda cfg, wait=True: False)
    monkeypatch.setattr(client, "stop_daemon", lambda cfg: False)
    monkeypatch.setattr(client, "run_index",
                        lambda cfg, action, files=None, corpus=None: {"added": 0})
    return fake_home
