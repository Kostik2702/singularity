"""Singularity install logic (cross-platform, phases per Integration_Setup_Plan).

Run via install.ps1 / install.sh wrappers (they create the venv and re-exec
this file with the venv python).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))


def phase1_deps() -> None:
    print("== Phase 1: python deps ==")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-r",
                           str(ENGINE / "requirements.txt")])
    # [imgqa] extras are part of the standard install: the imgqa console script
    # is registered unconditionally, so a plain -e install would leave it broken
    # (ModuleNotFoundError: PIL) on a fresh machine. Heavy ML stays out (OD-1).
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-e",
                           f"{ENGINE}[imgqa]"])


def _interactive() -> bool:
    """A terminal a human can answer from. CI, `sh -c`, a scheduled run and a
    piped installer are all non-interactive — prompting there would hang."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def choose_vault(explicit: str | None) -> Path | None:
    """Settle the vault path — by ASKING, not by guessing.

    Guessing is what this replaces: the engine used to carry one machine's
    absolute path as a built-in candidate, which outlived that machine's own
    move to another drive and shipped a dead drive letter to everyone else.
    The vault is the one thing an installer cannot infer, so it is the one
    thing it asks about.

    `--vault` wins outright and is created if missing — someone who names a
    path is stating where the vault should be. Without it, a resolved path is
    shown for confirmation rather than assumed, because a machine that already
    has a vault must not be silently re-pointed at another one. A
    non-interactive run never prompts: it fails with the flag to pass."""
    from singularity.config import resolve_vault

    if explicit:
        return _ensure_vault(Path(explicit).expanduser(), ask=False)

    found = resolve_vault(None)
    if not _interactive():
        if found is not None and found.exists():
            print(f"[install] vault: {found}")
            return found
        print(f"[install] vault not found: {found or '(nothing configured)'}",
              file=sys.stderr)
        print('[install] non-interactive run — pass the path explicitly: '
              './install/install.sh --vault "/path/to/your/vault"', file=sys.stderr)
        return None

    print("\n== Vault ==")
    print("Singularity keeps your notes in a vault — a plain folder of Markdown.")
    if found is not None and found.exists():
        print(f"Found one at: {found}")
        answer = input("Press Enter to use it, or type another path: ").strip()
        if not answer:
            return found
    else:
        if found is not None:
            print(f"Configured path does not exist: {found}")
        answer = input("Path to your vault (it will be created if missing): ").strip()

    while True:
        if answer:
            chosen = _ensure_vault(Path(answer.strip('"').strip("'")).expanduser(),
                                   ask=True)
            if chosen is not None:
                return chosen
        answer = input("Path to your vault: ").strip()


def _ensure_vault(path: Path, *, ask: bool) -> Path | None:
    """The chosen path, creating the directory when it is not there yet."""
    if path.is_dir():
        return path
    if path.exists():
        print(f"[install] not a directory: {path}", file=sys.stderr)
        return None
    if ask:
        reply = input(f"{path} does not exist. Create it? [Y/n] ").strip().lower()
        if reply and not reply.startswith("y"):
            return None
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[install] cannot create {path}: {exc}", file=sys.stderr)
        return None
    print(f"[install] created {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", help="vault path")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--skip-schedule", action="store_true")
    args = ap.parse_args(argv)

    # Fail fast BEFORE the slow dependency phase (github#1: a fresh macOS
    # install burned Phase 1 minutes, then died on the Windows default path).
    # config is stdlib-only, safe to import pre-deps.
    vault = choose_vault(args.vault)
    if vault is None:
        return 2

    if not args.skip_deps:
        phase1_deps()

    from singularity.config import load_config, write_pointer
    from singularity.cli import cmd_init

    print("== Phase 2: vault ==")
    # the CHOSEN vault, not args.vault — an interactive answer must win over
    # the flag that was not passed
    cfg = load_config(str(vault))
    write_pointer(cfg.vault)
    cmd_init(cfg, None)

    print("== Phase 3: local model install/adoption ==")
    cfg.apply_env()
    from singularity.search import client, model_store
    model = model_store.install(cfg)
    print(f"model: {model.get('source')} -> {model.get('snapshot')}")

    print("== Phase 3b: warm daemon + first index ==")
    client.stop_daemon(cfg)
    ok = client.start_daemon(cfg)
    print("daemon:", "up" if ok else "FAILED (see .mb/daemon.log)")
    if not ok:
        return 1
    stats = client.run_index(cfg, "rebuild")
    print(f"index: {stats}")

    print("== Phase 4: Claude Code hooks + skills + agents ==")
    failures: list[str] = []
    import register_hooks
    if register_hooks.main() != 0:
        failures.append("register_hooks")
    import register_skills
    if register_skills.main() != 0:
        failures.append("register_skills")
    import register_agents
    if register_agents.main() != 0:
        failures.append("register_agents")

    print("== Phase 4b: Codex profile + skills ==")
    import register_codex
    # register_codex.main() itself already returns 0 for the legitimate "Codex
    # not installed on this machine" skip — a nonzero here means Codex IS
    # present and registration genuinely failed, so it's a real failure too.
    if register_codex.main() != 0:
        failures.append("register_codex")

    print("== Phase 4c: Gemini profile + skills + SessionStart ==")
    import register_gemini
    # Same contract as Codex: absent client -> rc 0 (skip); a nonzero here means
    # Gemini IS present and registration genuinely failed.
    if register_gemini.main() != 0:
        failures.append("register_gemini")

    print("== Phase 4d: Grok profile + skills ==")
    import register_grok
    # Same contract: absent client -> rc 0 (skip); Singularity never downloads,
    # authenticates or upgrades Grok. A nonzero here means Grok IS present and
    # registration genuinely failed.
    if register_grok.main() != 0:
        failures.append("register_grok")

    print("== Phase 4e: CLIs on PATH ==")
    import register_path
    # Deliberately NOT a required phase: a machine whose PATH the user manages
    # themselves is a legitimate choice, and a read-only home should not fail an
    # otherwise complete install. Doctor's D7-cli-on-path reports the end state.
    if register_path.main() != 0:
        print("[install] PATH registration incomplete — `mb` may not be "
              "callable by name; see D7-cli-on-path below", file=sys.stderr)

    if not args.skip_schedule:
        print("== Phase 5: scheduler ==")
        from singularity.platform import scheduler
        from singularity import nightly as nightly_mod
        from singularity import watch as watch_mod
        # single source with `mb doctor --fix` (doctor.py C1/C2) so a re-install
        # on a machine that still has the legacy T02 "nightly-index" job
        # migrates it instead of leaving two competing scheduled jobs (T12),
        # and the watch job argv can't drift from what C2 verifies.
        print("nightly:", nightly_mod.migrate_scheduler_job(cfg))
        print("watch (at login):", scheduler.register(watch_mod.watch_job(cfg)))

    print("== Verify: mb doctor ==")
    from singularity import doctor
    doctor_rc = doctor.run(cfg, fix=True)

    # The install asked for the vault and nothing else, because nothing else
    # blocks a working install. That is exactly why the remaining answers have
    # to be named HERE: they are optional, so no failure will ever mention
    # them, and a machine that was never asked stays silently half-configured.
    from singularity import machine_setup

    lines = machine_setup.summary_lines(cfg.data, prefix="[install] ")
    if lines:
        print("== What this machine has not stated yet ==")
        for line in lines:
            print(line)

    if failures:
        print(f"[install] REQUIRED phase(s) failed: {', '.join(failures)} — "
              "installation incomplete, see errors above", file=sys.stderr)
        return 1
    return doctor_rc


if __name__ == "__main__":
    sys.exit(main())
