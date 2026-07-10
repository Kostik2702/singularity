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
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-e", str(ENGINE)])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--vault", help="vault path")
    ap.add_argument("--skip-deps", action="store_true")
    ap.add_argument("--skip-schedule", action="store_true")
    args = ap.parse_args()

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

    print("== Phase 4: Claude Code hooks + skills ==")
    import register_hooks
    register_hooks.main()
    import register_skills
    register_skills.main()

    print("== Phase 4b: Codex profile + skills ==")
    import register_codex
    register_codex.main()

    if not args.skip_schedule:
        print("== Phase 5: scheduler ==")
        from singularity.platform import scheduler
        nightly = scheduler.Job("nightly-index",
                                ["-m", "singularity", "--vault", str(cfg.vault),
                                 "index", "update"], daily_at="03:00")
        print("nightly-index:", scheduler.register(nightly))
        watch_job = scheduler.Job("watch",
                                  ["-m", "singularity", "--vault", str(cfg.vault),
                                   "watch"], at_login=True)
        print("watch (at login):", scheduler.register(watch_job))

    print("== Verify: mb doctor ==")
    from singularity import doctor
    return doctor.run(cfg, fix=True)


if __name__ == "__main__":
    sys.exit(main())
