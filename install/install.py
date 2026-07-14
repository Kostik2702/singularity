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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", help="vault path")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--skip-schedule", action="store_true")
    args = ap.parse_args(argv)

    # Fail fast BEFORE the slow dependency phase (github#1: a fresh macOS
    # install burned Phase 1 minutes, then died on the Windows default path).
    # config is stdlib-only, safe to import pre-deps.
    from singularity.config import resolve_vault
    vault = resolve_vault(args.vault)
    if vault is None or not vault.exists():
        print(f"[install] vault not found: {vault or '(nothing configured)'}", file=sys.stderr)
        print('[install] re-run with your vault path, e.g.: '
              './install/install.sh --vault "/path/to/your/vault"', file=sys.stderr)
        return 2

    if not args.skip_deps:
        phase1_deps()

    from singularity.config import load_config, write_pointer
    from singularity.cli import cmd_init

    print("== Phase 2: vault ==")
    cfg = load_config(args.vault)
    write_pointer(cfg.vault)
    cmd_init(cfg, None)

    print("== Phase 3: model warmup + first index ==")
    cfg.apply_env()
    from singularity.search import indexer, client
    stats = indexer.rebuild(cfg)
    print(f"index: {stats}")

    print("== Phase 3b: daemon ==")
    ok = client.start_daemon(cfg)
    print("daemon:", "up" if ok else "FAILED (see .mb/daemon.log)")

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

    if not args.skip_schedule:
        print("== Phase 5: scheduler ==")
        from singularity.platform import scheduler
        from singularity import nightly as nightly_mod
        # single source with `mb doctor --fix` (doctor.py C1) so a re-install
        # on a machine that still has the legacy T02 "nightly-index" job
        # migrates it instead of leaving two competing scheduled jobs (T12).
        print("nightly:", nightly_mod.migrate_scheduler_job(cfg))
        watch_job = scheduler.Job("watch",
                                  ["-m", "singularity", "--vault", str(cfg.vault),
                                   "watch"], at_login=True)
        print("watch (at login):", scheduler.register(watch_job))

    print("== Verify: mb doctor ==")
    from singularity import doctor
    doctor_rc = doctor.run(cfg, fix=True)

    if failures:
        print(f"[install] REQUIRED phase(s) failed: {', '.join(failures)} — "
              "installation incomplete, see errors above", file=sys.stderr)
        return 1
    return doctor_rc


if __name__ == "__main__":
    sys.exit(main())
