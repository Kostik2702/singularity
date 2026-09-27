"""mb doctor — installation verification.

Statuses: PASS / WARN / FAIL / FIXED / SKIP / AUTH. Guarded checks (one
failure never aborts the run), exit 0 unless there is a hard FAIL.
Full specification: vault 04_Plans/Doctor_Plan. This is the M0-M2 subset:
blocks A (structure/config), B (search core), C (scheduler), D (claude/CLI).
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .platform import current_os, scheduler

MEMORY_FOLDERS = ["learnings/active", "learnings/archive", "decisions", "howto",
                  "incidents", "rules", "reference", "changelog", "templates", "_moc"]


@dataclass
class Report:
    checks: list[dict] = field(default_factory=list)
    quiet: bool = False   # suppress human lines (used by --json)

    def add(self, check_id: str, status: str, detail: str = "", fix: str = "") -> None:
        self.checks.append({"id": check_id, "status": status,
                            "detail": detail, "fix": fix})
        if not self.quiet:
            print(f"[{status}] {check_id}: {detail}")

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for c in self.checks:
            out[c["status"]] = out.get(c["status"], 0) + 1
        return out

    @property
    def has_fail(self) -> bool:
        return any(c["status"] == "FAIL" for c in self.checks)


def _guard(rep: Report, check_id: str, fn) -> None:
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 — doctor checks are guarded by design
        rep.add(check_id, "FAIL", f"check crashed: {exc}")


def _load_install(name: str):
    """Load a module from install/ (not an importable package) for its constants
    and safe helpers — the single source, so doctor can't drift from the registrar."""
    import importlib.util
    engine = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(name, engine / "install" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _action_matches(state: dict, expected_args: str | None) -> bool:
    """Does the scheduler's ACTUAL stored action match the argv we intended?
    Returns True when uncomparable (non-Windows read-back) so we never raise a
    false alarm; False only on a concrete mismatch (e.g. a vault path that lost
    its quoting and was split — the exact pre-AUD-01 breakage)."""
    actual = state.get("arguments")
    if expected_args is None or actual is None:
        return True
    return actual.strip() == expected_args.strip()


def _run_shim(sh: str, shim: Path, cwd: str | None = None) -> tuple[int, str, str]:
    """Run the POSIX shim through sh; separated so tests can stub the subprocess.

    `cwd` is explicit because the failure this check exists to catch is
    CWD-dependent (issue #16), so inheriting doctor's own directory made the
    verdict depend on where the user happened to stand."""
    proc = subprocess.run([sh, str(shim), "--version"],
                          capture_output=True, text=True, timeout=60, cwd=cwd)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


# Watcher is considered alive if its heartbeat file was touched this recently.
WATCH_HEARTBEAT_MAX_AGE_S = 15 * 60


def _paths_fix(keys: list[str]) -> str:
    """The fix line for A6, quoting a key that is actually broken.

    The generic `mb config set code_repos_dir "<path>"` example was worse than
    unhelpful: none of the keys A6 reports are flat, and until 2026-08-04 `mb
    config set` did not resolve dotted keys at all — following the printed fix
    wrote a literal top-level key, reported success, and left the WARN in
    place. It resolves them now; the example points at a real key so the two
    stay honest about each other."""
    plain = [k for k in keys if not k.startswith(("projects.", "llm.models."))]
    line = (f"set each one for this machine, e.g. "
            f'mb config set {plain[0] if plain else keys[0]} "<path>"  '
            f"(the correct values cannot be guessed)")
    # Both of these own a registry with its own commands — sending someone to
    # `config set` for them is sending them the long way round, and for a model
    # that no longer exists on this machine there is no `config set` answer at
    # all: the entry has to go, which only `mb llm remove` does.
    if any(k.startswith("projects.") for k in keys):
        line += "; repoint projects with `mb project add <id> <path>`"
    if any(k.startswith("llm.models.") for k in keys):
        line += ("; models with `mb llm add <name> <gguf>` "
                 "(or `mb llm remove <name>` if it is gone for good)")
    return line


def config_paths_block(rep: Report, cfg: Config) -> None:
    """Check A6: does every absolute path in the config belong to THIS machine?

    Issue #14: the vault is built to travel, `.mb/config.json` travels with it,
    and every absolute path inside it is machine-specific. Only
    `code_repos_dir` was ever validated (by K1), so `llm.server_bin`, the gguf
    paths, the `--chat-template-file` inside `llm.models.*.args` and every
    `projects[].path` arrived dead on a new machine and went unmentioned —
    each surfacing later as its own mid-session failure, weeks apart.

    Module-level (not a run() closure) so it is unit-testable, like the client
    blocks below."""
    from .machine import config_paths

    entries = config_paths.path_entries(cfg.data)
    if not entries:
        rep.add("A6-config-paths", "SKIP", "no absolute paths in the config")
        return
    foreign: dict[str, list[str]] = {}
    missing: list[str] = []
    for key, value in entries:
        other = config_paths.foreign_os(value)
        if other:
            foreign.setdefault(other, []).append(key)
        elif not Path(value).exists():
            missing.append(key)
    if foreign:
        # ONE understandable statement instead of N confusing absences: a
        # `C:\…` on macOS is not a missing path, it is another machine's.
        os_name = "Windows" if "windows" in foreign else "POSIX"
        keys = sorted(k for ks in foreign.values() for k in ks)
        rep.add("A6-config-paths", "WARN",
                f"config contains {os_name} paths — this vault was last "
                f"configured on another operating system; "
                f"{len(keys)} key(s) point nowhere on this machine: "
                f"{', '.join(keys)}"
                + (f" (plus {len(missing)} more missing: "
                   f"{', '.join(sorted(missing))})" if missing else ""),
                fix=_paths_fix(keys))
        return
    if missing:
        rep.add("A6-config-paths", "WARN",
                f"{len(missing)} configured path(s) do not exist on this "
                f"machine: {', '.join(sorted(missing))}",
                fix=_paths_fix(sorted(missing)))
        return
    rep.add("A6-config-paths", "PASS",
            f"{len(entries)} configured path(s) all resolve on this machine")


def gemini_block(rep: Report, fix: bool) -> None:
    """Block E: verify the Gemini integration on a WORKING path (version, managed
    profile, portable skill targets, settings parse, named hook), not mere file
    presence. `--fix` runs ONLY the safe registrar (no network/auth). Absent client
    is a single SKIP. Module-level (not a run() closure) so it is unit-testable."""
    rg = _load_install("register_gemini")
    home_g = Path.home() / ".gemini"
    exe, where = rg._gemini_exe()
    if not (home_g.exists() or exe is not None):
        rep.add("E1-gemini", "SKIP", "Gemini CLI not installed on this machine")
        return
    version = rg._gemini_version()

    def profile_ok() -> bool:
        p = home_g / "GEMINI.md"
        return p.exists() and rg.BEGIN in p.read_text(encoding="utf-8", errors="ignore")

    def missing_skills() -> list[str]:
        d = home_g / "skills"
        return [n for n in rg.GEMINI_SKILLS if not (d / n).exists()]

    def hook_state():
        s = home_g / "settings.json"
        if not s.exists():
            return False
        try:
            data = json.loads(s.read_text(encoding="utf-8-sig"))   # BOM-tolerant
        except json.JSONDecodeError:
            return "malformed"
        hooks = data.get("hooks", {})

        def _names(event):
            return [h.get("name") for dfn in hooks.get(event, []) for h in dfn.get("hooks", [])]

        return (rg.SESSION_START_HOOK_NAME in _names("SessionStart")
                and rg.AFTER_AGENT_HOOK_NAME in _names("AfterAgent"))

    # "Located but unreadable version" is a repairable state, not a dead end
    # (issue #10): the old gate was `supported`, which is False whenever the
    # version is unknown, so `--fix` could not reach the hook branch in exactly
    # the situation that needed it most.
    supported = version is not None and version >= rg.MIN_HOOKS_VERSION
    repairable = supported or (version is None and exe is not None)

    # --fix runs ONLY the safe registrar (no network/auth) once, then re-checks.
    fixed = False
    if fix and (not profile_ok() or missing_skills()
                or (repairable and hook_state() is not True)):
        rg.main()
        fixed = True

    if exe is None:
        rep.add("E1-gemini", "WARN",
                f"{home_g} exists but the `gemini` binary is unreachable from "
                f"this process — PATH and the nvm/volta/fnm/homebrew locations "
                f"were all checked",
                fix="re-run `mb doctor --fix` from an interactive shell "
                    "(a version manager is a shell function, invisible here)")
    elif version is None:
        rep.add("E1-gemini", "WARN",
                f"installed at {exe}, but `gemini --version` unreadable")
    elif not supported:
        rep.add("E1-gemini", "WARN",
                f"gemini {'.'.join(map(str, version))} < "
                f"{'.'.join(map(str, rg.MIN_HOOKS_VERSION))} (skills/hooks may be ignored)")
    else:
        rep.add("E1-gemini", "PASS", f"gemini {'.'.join(map(str, version))}")

    if profile_ok():
        rep.add("E2-gemini-profile", "FIXED" if fixed else "PASS",
                "managed GEMINI.md block present")
    else:
        rep.add("E2-gemini-profile", "WARN", "managed GEMINI.md block missing",
                fix="python install/register_gemini.py")

    miss = missing_skills()
    if not miss:
        rep.add("E3-gemini-skills", "FIXED" if fixed else "PASS",
                f"{len(rg.GEMINI_SKILLS)} portable skills deployed")
    else:
        rep.add("E3-gemini-skills", "WARN", f"skills not deployed: {', '.join(miss)}",
                fix="python install/register_gemini.py")

    h = hook_state()
    if h == "malformed":
        rep.add("E4-gemini-hooks", "FAIL", "~/.gemini/settings.json unparseable",
                fix="repair or remove the file, then mb doctor --fix")
    elif exe is None:
        # NOT a version SKIP: naming a constraint the installed client may well
        # satisfy is what made this take a week to diagnose (issue #10).
        rep.add("E4-gemini-hooks", "WARN",
                "hook support could not be established — the `gemini` binary is "
                "unreachable from this process, so recall and capture are off",
                fix="mb doctor --fix from an interactive shell")
    elif not repairable:
        rep.add("E4-gemini-hooks", "SKIP",
                f"SessionStart hook requires gemini >= "
                f"{'.'.join(map(str, rg.MIN_HOOKS_VERSION))} "
                f"(this one reports {'.'.join(map(str, version))})")
    elif h is True:
        rep.add("E4-gemini-hooks", "FIXED" if fixed else "PASS",
                "SessionStart + AfterAgent hooks registered"
                + ("" if supported else " (version unreadable — inert if unsupported)"))
    else:
        rep.add("E4-gemini-hooks", "WARN", "SessionStart/AfterAgent hooks not registered",
                fix="python install/register_gemini.py")


def grok_block(rep: Report, fix: bool) -> None:
    """Block F: verify the Grok Build CLI integration on a WORKING path (version,
    managed AGENTS.md block, portable skill targets), not mere file presence.
    Default path is local, read-only, auth-free and network-free: the probe is
    `grok --version` — never a session (GRK-00: sessions auto-connect real MCP
    servers even from an isolated GROK_HOME). `--fix` runs ONLY the safe
    registrar. Absent client is a single SKIP. Runtime capture hooks are not
    deployed for Grok yet (GRK-04) — reported as SKIP, never a false PASS.
    Module-level (not a run() closure) so it is unit-testable."""
    rg = _load_install("register_grok")
    grok_home = rg._grok_home()
    if not rg._grok_present():
        rep.add("F1-grok", "SKIP", "Grok CLI not installed on this machine")
        return
    exe, _where = rg._grok_exe()
    version = rg._grok_version()

    def profile_ok() -> bool:
        p = grok_home / "AGENTS.md"
        return p.exists() and rg.BEGIN in p.read_text(encoding="utf-8", errors="ignore")

    def missing_skills() -> list[str]:
        d = grok_home / "skills"
        return [n for n in rg.GROK_SKILLS if not (d / n).exists()]

    def hook_state():
        p = grok_home / "hooks" / rg.HOOK_FILENAME
        if not p.exists():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8-sig"))   # BOM-tolerant read
        except json.JSONDecodeError:
            return "malformed"
        for dfn in data.get("hooks", {}).get("Stop", []):
            for h in dfn.get("hooks", []):
                if rg.HOOK_SCRIPT_MARKER in str(h.get("command", "")):
                    return True
        return False

    supported = version is not None and version >= rg.MIN_GROK_VERSION

    # --fix runs ONLY the safe registrar (no network/auth) once, then re-checks.
    fixed = False
    if fix and (not profile_ok() or missing_skills()
                or (supported and hook_state() is not True)):
        rg.main()
        fixed = True

    if exe is None:
        rep.add("F1-grok", "WARN",
                f"{grok_home} exists but the `grok` binary is unreachable from "
                f"this process — PATH and the nvm/volta/fnm/homebrew locations "
                f"were all checked",
                fix="re-run `mb doctor --fix` from an interactive shell "
                    "(a version manager is a shell function, invisible here)")
    elif version is None:
        rep.add("F1-grok", "WARN",
                f"installed at {exe}, but `grok --version` unreadable")
    elif version < rg.MIN_GROK_VERSION:
        rep.add("F1-grok", "WARN",
                f"grok {'.'.join(map(str, version))} < "
                f"{'.'.join(map(str, rg.MIN_GROK_VERSION))} (GRK-00 contract unproven below this)")
    else:
        rep.add("F1-grok", "PASS", f"grok {'.'.join(map(str, version))}")

    if profile_ok():
        rep.add("F2-grok-profile", "FIXED" if fixed else "PASS",
                f"managed AGENTS.md block present in {grok_home}")
    else:
        rep.add("F2-grok-profile", "WARN", "managed AGENTS.md block missing",
                fix="python install/register_grok.py")

    miss = missing_skills()
    if not miss:
        rep.add("F3-grok-skills", "FIXED" if fixed else "PASS",
                f"{len(rg.GROK_SKILLS)} portable skills deployed")
    else:
        rep.add("F3-grok-skills", "WARN", f"skills not deployed: {', '.join(miss)}",
                fix="python install/register_grok.py")

    # Runtime capability: the Stop capture nudge (GRK-04). Dynamic SessionStart
    # recall stays a documented NO-GO (GRK-00: no context-injection channel) —
    # the static AGENTS.md instruction covered by F2 is the recall path.
    h = hook_state()
    if h == "malformed":
        rep.add("F4-grok-capture", "FAIL",
                f"{grok_home / 'hooks' / rg.HOOK_FILENAME} unparseable",
                fix="repair or remove the file" + (
                    ", then mb doctor --fix" if supported else
                    " (re-registration needs grok >= "
                    f"{'.'.join(map(str, rg.MIN_GROK_VERSION))})"))
    elif exe is None:
        # Not a version SKIP — the version is not what is missing here (#10).
        rep.add("F4-grok-capture", "WARN",
                "the stop-gate contract could not be established — the `grok` "
                "binary is unreachable from this process, so capture is off",
                fix="mb doctor --fix from an interactive shell")
    elif not supported:
        rep.add("F4-grok-capture", "SKIP",
                f"Stop capture hook requires grok >= {'.'.join(map(str, rg.MIN_GROK_VERSION))} "
                + (f"(this one reports {'.'.join(map(str, version))})" if version
                   else "(`grok --version` unreadable, so the contract is unproven)"))
    elif h is True:
        rep.add("F4-grok-capture", "FIXED" if fixed else "PASS",
                "Stop capture hook registered")
    else:
        rep.add("F4-grok-capture", "WARN", "Stop capture hook not registered",
                fix="python install/register_grok.py")


def wiki_loop_block(rep: Report, cfg: Config, fix: bool) -> None:
    """Block W — the ingest layer (WIKI-LOOP W11).

    Deliberately the strictest block in this file. Everything else under
    `.mb/` is derived state that a rebuild recreates; `.mb/sources/` is the
    only content there, and the failure mode it guards against — "delete .mb
    and re-index" — is a habit, not an accident. So "the registry lists
    documents whose files are gone" is a FAIL, not a WARN.

    `--fix` here is strictly additive: it may recreate an empty registry or
    catch the corpus up, and it NEVER deletes a raw file."""
    from .knowledge import sources as sources_mod

    registry = sources_mod.read_registry(cfg)["sources"]
    stored = {sid: rec for sid, rec in registry.items()
              if isinstance(rec, dict) and rec.get("stored")}
    on_disk = {p.relative_to(cfg.vault).as_posix()
               for p in cfg.sources_dir.rglob("*.md")} if cfg.sources_dir.exists() else set()

    def w1():
        if not registry:
            if on_disk:
                rep.add("W1-sources-integrity", "WARN",
                        f"{len(on_disk)} raw file(s) present but the registry is empty",
                        fix="mb ingest backfill --apply, or restore .mb/sources.json "
                            "from `mb backup list`")
            else:
                rep.add("W1-sources-integrity", "PASS", "no sources ingested yet")
            return
        if stored and not cfg.sources_dir.exists():
            rep.add("W1-sources-integrity", "FAIL",
                    f"registry lists {len(stored)} stored source(s) but "
                    f"{cfg.sources_dir} does not exist — raw data is the one thing "
                    f"a rebuild cannot recreate",
                    fix="mb backup list && mb backup restore <id>  (restore only ADDS files)")
            return
        missing = sorted(sid for sid, rec in stored.items()
                         if str(rec.get("stored_path") or "") not in on_disk)
        if missing:
            rep.add("W1-sources-integrity", "FAIL",
                    f"{len(missing)} registry entr(ies) have no raw file: "
                    f"{', '.join(missing[:5])}",
                    fix="mb backup restore <id>, or `mb ingest export` a surviving copy")
            return
        orphans = sorted(on_disk - {str(rec.get("stored_path")) for rec in stored.values()})
        if orphans:
            rep.add("W1-sources-integrity", "WARN",
                    f"{len(orphans)} raw file(s) with no registry entry: "
                    f"{', '.join(orphans[:3])}",
                    fix="re-add them with `mb ingest add` (write-once: the existing "
                        "file is kept, only the record is recreated)")
        else:
            rep.add("W1-sources-integrity", "PASS",
                    f"{len(stored)} source(s), registry and disk agree")

    def w2():
        from .search import indexer

        if not cfg.get("ingest_index_sources"):
            rep.add("W2-sources-index", "SKIP", "ingest_index_sources=false")
            return
        if not stored:
            rep.add("W2-sources-index", "PASS", "nothing to index yet")
            return
        summary = indexer.index_summary(cfg.sources_index_dir)
        if summary is None:
            msg = "sources corpus not built — raw documents are not searchable"
            if fix:
                try:
                    from .search import client
                    client.run_index(cfg, "rebuild", corpus="sources")
                    rep.add("W2-sources-index", "FIXED", "built the sources corpus")
                    return
                except Exception as exc:  # noqa: BLE001 — reported, never fatal
                    rep.add("W2-sources-index", "WARN", f"{msg} ({exc})",
                            fix="mb ingest index --rebuild")
                    return
            rep.add("W2-sources-index", "WARN", msg, fix="mb ingest index --rebuild")
            return
        memory = indexer.index_summary(cfg.memory_index_dir) or {}
        if memory.get("model") and summary.get("model") \
                and memory["model"] != summary["model"]:
            rep.add("W2-sources-index", "WARN",
                    f"corpora built by different models "
                    f"(vault {memory['model']!r} vs sources {summary['model']!r}); "
                    f"source search and `mb ingest plan` refuse rather than compare "
                    f"across embedding spaces",
                    fix="mb ingest index --rebuild")
            return
        indexed_at = str(summary.get("indexed_at") or "")
        newest = max((str(rec.get("ingested_at") or "") for rec in stored.values()),
                     default="")
        if newest and indexed_at and newest > indexed_at:
            rep.add("W2-sources-index", "WARN",
                    f"corpus is older than the registry (indexed {indexed_at}, "
                    f"newest source {newest}) — the watcher does not see .mb/",
                    fix="mb ingest index")
            return
        rep.add("W2-sources-index", "PASS",
                f"{summary.get('chunk_count')} chunk(s), model matches the vault index")

    def w3():
        if not cfg.git_dir.exists():
            rep.add("W3-sources-portability", "SKIP", "vault is not a git repo")
            return
        git = shutil.which("git")
        if git is None:
            rep.add("W3-sources-portability", "SKIP", "git not on PATH")
            return
        try:
            proc = subprocess.run(
                [git, "check-ignore", "-q", ".mb/sources"],
                cwd=str(cfg.vault), capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            rep.add("W3-sources-portability", "SKIP", "git check-ignore unavailable")
            return
        if proc.returncode == 0:
            rep.add("W3-sources-portability", "WARN",
                    ".mb/sources is git-ignored — moving this vault to another "
                    "machine would leave every ingested document behind",
                    fix="un-ignore .mb/sources/, or keep an `mb ingest export <dir>` "
                        "copy somewhere that IS synced")
        else:
            rep.add("W3-sources-portability", "PASS", ".mb/sources travels with the vault")

    _guard(rep, "W1", w1)
    _guard(rep, "W2", w2)
    _guard(rep, "W3", w3)


def code_index_block(rep: Report, cfg: Config, fix: bool, heavy: bool) -> None:
    """Block K — the code corpus (CODE-INDEX C05).

    Deliberately the LEAST strict block in this file, the mirror image of
    block W. `.mb/sources/` holds the only copy of raw documents, so its
    failures are FAIL; `.mb/code_index/` is 100% derived from files that live
    in git — delete it and one command rebuilds it. Nothing here can therefore
    justify failing `mb doctor`, and no scenario returns FAIL.

    Staleness is the interesting signal: with no watcher over repositories
    (decision D4), this corpus only moves when nightly runs or when someone
    asks. A stale code index answers with yesterday's implementation, which is
    worse than answering nothing."""
    from .search import indexer

    def k1():
        if cfg.code_repos_dir is None:
            rep.add("K1-code-index", "SKIP",
                    "code corpus off (code_repos_dir not set)")
            return
        root = cfg.code_repos_dir
        if not root.exists():
            rep.add("K1-code-index", "WARN",
                    f"code_repos_dir points at a path that does not exist: {root}",
                    fix='mb config set code_repos_dir "<path to your repositories>"')
            return
        summary = indexer.index_summary(cfg.code_index_dir)
        if summary is None:
            msg = "code corpus configured but never built — `--with-code` has " \
                  "nothing to search"
            # gated behind --heavy like the model/index repairs: building this
            # loads the encoder and takes minutes, which is not what a bare
            # `mb doctor --fix` should silently start
            if fix and heavy:
                try:
                    from .search import client
                    client.run_index(cfg, "rebuild", corpus="code")
                    rep.add("K1-code-index", "FIXED", "built the code corpus")
                    return
                except Exception as exc:  # noqa: BLE001 — reported, never fatal
                    rep.add("K1-code-index", "WARN", f"{msg} ({exc})",
                            fix="mb index rebuild --code")
                    return
            rep.add("K1-code-index", "WARN", msg,
                    fix="mb index rebuild --code  (or mb doctor --fix --heavy)")
            return
        memory = indexer.index_summary(cfg.memory_index_dir) or {}
        if memory.get("model") and summary.get("model") \
                and memory["model"] != summary["model"]:
            rep.add("K1-code-index", "WARN",
                    f"corpora built by different models "
                    f"(vault {memory['model']!r} vs code {summary['model']!r}); "
                    f"code search refuses rather than compare across embedding "
                    f"spaces",
                    fix="mb index rebuild --code")
            return
        if not summary.get("file_count"):
            # issue #13: "built, but from nothing" was falling through to the
            # success branch and reporting `PASS: 0 chunk(s) from 0 file(s)` —
            # a message that contradicts its own status. This check exists to
            # answer "can I search my code", and here the answer is no. It is
            # also the check that would have caught the stale-daemon-config bug
            # immediately; instead it certified the broken state as healthy.
            rep.add("K1-code-index", "WARN",
                    f"index built from 0 files under {root} — nothing matched "
                    f"the indexable patterns, or every repository was pruned",
                    fix="check code_repos_dir and code_skip_dirs, and that the "
                        "daemon picked up the current config "
                        "(mb daemon restart), then mb index rebuild --code")
            return
        files = indexer.index_files(cfg.code_index_dir)
        age_h = None
        if files is not None:
            age_h = (time.time() - files[0].stat().st_mtime) / 3600
        max_age_h = 24 * float(cfg.get("code_index_max_age_days") or 7)
        if age_h is not None and age_h > max_age_h:
            rep.add("K1-code-index", "WARN",
                    f"code corpus is {age_h / 24:.1f} days old — code changes far "
                    f"more often than notes and has no watcher (D4)",
                    fix="mb index update --code  (nightly does this automatically)")
            return
        rep.add("K1-code-index", "PASS",
                f"{summary.get('chunk_count')} chunk(s) from "
                f"{summary.get('file_count')} file(s), model matches the vault index")

    _guard(rep, "K1", k1)


# Block K2 thresholds, each with the measurement behind it.
# Files tree-sitter parsed with recovered errors: 0.12% on the council's 100k-file
# proxy, 0.18% (78 of 44,294) on the current one, none in this machine's other nine
# units. A grammar that stops fitting the code -- new syntax, a file type handed the
# wrong grammar -- shows as a jump in that share, never as a failure, because
# tree-sitter recovers. 2% is ten times the measured share. Under ten such files it is
# one or two broken files in a small unit, not a grammar.
_GRAPH_DEGRADED_SHARE = 0.02
_GRAPH_DEGRADED_MIN_FILES = 10
# PRAGMA quick_check reads the whole file: 1.2-2.0 s on the 984 MB proxy graph with the
# file in the OS cache, under 0.6 s for every other unit here. The budget bounds doctor
# on a cold or slow disk; a check cut off by it is said in the detail, not called damage.
_GRAPH_QUICK_CHECK_SECONDS = 10.0
# 22 KB per indexed file (984 MB / 44,294 files on the proxy, 24 KB on this engine), so
# the plan's largest unit, 100k files, is about 2.2 GB. A store over 5 GB holds more than
# twice that: generations nobody collected, or a scope that took in a vendored tree.
_GRAPH_DISK_WARN_BYTES = 5 * 1024 ** 3


def _dir_bytes(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _quick_check(con, budget: float) -> str | None:
    """PRAGMA quick_check's first line ('ok' when intact), or None when it
    ran past `budget` seconds and was interrupted."""
    import sqlite3

    deadline = time.monotonic() + budget
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
    except sqlite3.OperationalError as exc:
        if "interrupt" in str(exc):
            return None
        raise
    finally:
        con.set_progress_handler(None, 0)
    return str(row[0]) if row else "no answer"


_K2_FIX = {
    "damaged": "the graph is derived: delete the unit's directory under code_graph/ and "
               "`mb code build` it again",
    "rebuild": "`mb code build` from inside the repository",
    "stale": "`mb code update` from inside the repository",
    "degraded": "`mb code schema` shows parse_status; compare requirements-graph.txt pins with the code",
    "audit": "`mb code audit` lists them, masked; remove the value from the source, then `mb code build`",
    "unaudited": "`mb code audit` (a build audits too)",
    "orphans": "the unit's next `mb code build` removes them; if they stay, close what holds them "
               "open (another `mb code`, a database browser)",
    "disk": "`mb stats` shows each graph's size; delete the ones you no longer need under code_graph/",
    "parser": "python -m pip install --require-hashes -r requirements-graph.txt",
    "build": "`mb code build --all`",
}


def _code_graph_unit(cfg: Config, card: dict, parser_ok: bool) -> tuple[list[tuple[str, str]], list[str], int]:
    """([(problem, fix key)], [notes], files) of one built unit."""
    from .codegraph import audit, store, versions

    problems: list[tuple[str, str]] = []
    notes: list[str] = []
    db = store.current_db(cfg, card["key"])
    if db is None:
        return [("no current generation: CURRENT is missing or names one that is gone", "rebuild")], notes, 0
    con = store.connect_ro(db)
    try:
        qc = _quick_check(con, _GRAPH_QUICK_CHECK_SECONDS)
        if qc is None:
            notes.append(f"{card['key']}: quick_check stopped after {_GRAPH_QUICK_CHECK_SECONDS:.0f}s, not finished")
        elif qc != "ok":
            problems.append((f"quick_check: {qc}", "damaged"))
        meta = store.read_meta(con)
        if meta.get("schema_version") != store.SCHEMA_VERSION:
            # the tables below differ between schemas: nothing more is read from this graph
            problems.append((f"schema {meta.get('schema_version')}, this engine writes {store.SCHEMA_VERSION}",
                             "rebuild"))
            return problems, notes, int(meta.get("files") or 0)
        mismatch = versions.version_mismatch(meta) if parser_ok else {}
        if mismatch:
            problems.append(("written by other parser/resolver versions ("
                             + ", ".join(f"{k} {a}->{b}" for k, (a, b) in sorted(mismatch.items()))
                             + "): changed files are not written in", "rebuild"))
        stale = con.execute("SELECT count(*) FROM stale").fetchone()[0]
        if meta.get("stale_all"):
            problems.append((f"stale as a whole: {meta['stale_all']}", "stale"))
        elif stale:
            problems.append((f"{stale} stale file(s) waiting to be resolved again", "stale"))
        status = dict(con.execute("SELECT parse_status, count(*) FROM files GROUP BY parse_status").fetchall())
        files, partial = sum(status.values()), status.get("partial", 0)
        if partial >= _GRAPH_DEGRADED_MIN_FILES and partial / files > _GRAPH_DEGRADED_SHARE:
            problems.append((f"{partial} of {files} files parsed with errors ({partial / files:.1%}, "
                             f"over {_GRAPH_DEGRADED_SHARE:.0%})", "degraded"))
        rec = audit.recorded(meta)
        if rec is None:
            problems.append(("never audited for secrets", "unaudited"))
        else:
            if rec["findings"]:
                kinds = ", ".join(f"{k} {n}" for k, n in (rec.get("by_kind") or {}).items())
                problems.append((f"{rec['findings']} secret-shaped value(s) stored ({kinds})", "audit"))
            if rec.get("allowed"):
                notes.append(f"{card['key']}: {rec['allowed']} audit finding(s) accepted by code_graph_audit_allow")
            since = int(meta.get("seq") or 0) - int(rec.get("at_seq") or 0)
            if since:
                notes.append(f"{card['key']}: {since} in-place update(s) since the last audit")
        return problems, notes, files
    finally:
        con.close()


def code_graph_block(rep: Report, cfg: Config) -> None:
    """Block K2 — the structural code graph (CODE-GRAPH D1, P6).

    WARN only, never FAIL, by decision. Like the code corpus of K1 the graph
    is derived state: delete `code_graph/` and `mb code build --all` makes it
    again, so nothing found here can justify failing `mb doctor` -- and a
    graph this check cannot read is a WARN on that graph, not a crash.

    The secret audit is not re-run here: it takes ~25 s on a 44k-file graph.
    Every full build and every `mb code audit` records its count in the
    graph's meta; that is what is read, with the in-place updates written
    since. A graph never audited is a WARN: the audit is mandatory (D1)."""
    import sys

    def k2():
        from .codegraph import store, versions

        cards = store.built_units(cfg)
        if not cfg.data.get("code_graph_enabled"):
            rep.add("K2-code-graph", "SKIP", "code graph off (code_graph_enabled is false)"
                    + (f"; {len(cards)} graph(s) still on disk under {cfg.code_graph_dir}" if cards else ""))
            return
        root = cfg.code_graph_dir
        problems: dict[str, list[tuple[str, str]]] = {}
        notes: list[str] = []
        parser_ok = versions.parser_available()
        if not parser_ok:
            problems[sys.executable] = [("the parser is not installed here: queries answer without "
                                         "writing changed files in", "parser")]
        files = 0
        for card in cards:
            try:
                found, said, n = _code_graph_unit(cfg, card, parser_ok)
            except Exception as exc:  # noqa: BLE001 — an unreadable graph is a WARN on it, never a FAIL
                found, said, n = [(f"could not be read: {exc}", "damaged")], [], 0
            files += n
            notes += said
            if found:
                problems.setdefault(card["key"], []).extend(found)
        names = {store.unit_dir(cfg, c["key"]).name: c["key"] for c in cards}
        for d in sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []:
            lock = d / ".build.lock"
            if lock.exists() and not store._owner_is_dead(lock):
                notes.append(f"{names.get(d.name, d.name)}: a build is running")
                continue
            try:
                current = (d / "CURRENT").read_text(encoding="utf-8").strip()
            except OSError:
                current = None
            left = [g for g in d.glob("gen_*") if g.is_dir() and g.name != current]
            if left:
                size = sum(_dir_bytes(g) for g in left)
                problems.setdefault(names.get(d.name, d.name), []).append(
                    (f"{len(left)} old generation(s), {size / 1e6:.0f} MB, not collected", "orphans"))
        disk = _dir_bytes(root) if root.is_dir() else 0
        if disk > _GRAPH_DISK_WARN_BYTES:
            problems.setdefault(str(root), []).append(
                (f"{disk / 1e9:.1f} GB, over {_GRAPH_DISK_WARN_BYTES / 1024 ** 3:.0f} GB", "disk"))
        if not cards and not problems:
            rep.add("K2-code-graph", "WARN", "code graph on but no graph built — `mb code` has nothing to answer from",
                    fix=_K2_FIX["build"])
            return
        head = f"{len(cards)} graph(s), {files} files, {disk / 1e6:.0f} MB"
        tail = ("; " + "; ".join(notes)) if notes else ""
        if problems:
            detail = " | ".join(f"{unit}: {', '.join(p for p, _ in found)}" for unit, found in problems.items())
            fixes = dict.fromkeys(fix for found in problems.values() for _, fix in found)
            rep.add("K2-code-graph", "WARN", f"{head} — {detail}{tail}",
                    fix=" | ".join(_K2_FIX[f] for f in fixes))
            return
        rep.add("K2-code-graph", "PASS", f"{head}: current, intact, audited clean{tail}")

    # Not `_guard`: its crash status is FAIL, and this block is WARN only.
    try:
        k2()
    except Exception as exc:  # noqa: BLE001 — doctor checks are guarded by design
        rep.add("K2-code-graph", "WARN", f"check crashed: {exc}")


def run(cfg: Config, fix: bool = False, heavy: bool = False,
        as_json: bool = False) -> int:
    rep = Report(quiet=as_json)

    # ---- Block A: structure + config ---------------------------------
    def a1():
        rep.add("A1-config", "PASS", str(cfg.config_path))

    def a2():
        if cfg.obsidian_dir.exists():
            rep.add("A2-vault-obsidian", "PASS", str(cfg.vault))
        else:
            rep.add("A2-vault-obsidian", "WARN",
                    "no .obsidian/ — vault not opened in Obsidian yet")

    def a3():
        missing = [f for f in MEMORY_FOLDERS if not cfg.memory_folder(f).exists()]
        if not missing:
            rep.add("A3-folders", "PASS", "memory folder tree complete")
        elif fix:
            for f in missing:
                cfg.memory_folder(f).mkdir(parents=True, exist_ok=True)
            rep.add("A3-folders", "FIXED", f"created: {', '.join(missing)}")
        else:
            rep.add("A3-folders", "WARN", f"missing: {', '.join(missing)}",
                    fix="mb doctor --fix")

    def a4():
        if cfg.mb_dir.exists():
            rep.add("A4-mb-dir", "PASS", str(cfg.mb_dir))
        else:
            cfg.mb_dir.mkdir(parents=True, exist_ok=True)
            rep.add("A4-mb-dir", "FIXED", "created .mb/")

    def a5():
        from .platform import secrets
        be = secrets.backend()
        if be == "keyring":
            rep.add("A5-secrets", "PASS", "secure keyring backend")
        else:
            rep.add("A5-secrets", "WARN",
                    "no OS keyring — secrets require explicit plaintext opt-in",
                    fix="install a keyring backend for secure secret storage")

    def a7():
        # 0.7: the config is per machine. A vault that still holds one means
        # either the migration has not run yet, or something wrote a config
        # back into a folder that travels between machines — the shape that
        # made nine keys arrive dead after one clone (issue #14).
        from .config import legacy_config_path

        legacy = legacy_config_path(cfg.vault)
        if not legacy.exists():
            rep.add("A7-config-location", "PASS",
                    f"config is per machine ({cfg.config_path})")
            return
        rep.add("A7-config-location", "WARN",
                f"a config still sits inside the vault: {legacy} — it travels "
                f"with the vault, so its absolute paths are wrong on any other "
                f"machine",
                fix="run any `mb` command to adopt it automatically, "
                    "or delete it once the machine config is correct")

    def a8():
        # A6 grades the values that ARE there. Since the config became per
        # machine, the commoner shape is a value that is not there at all: the
        # vault arrives with the settings and without the local answers, and
        # nothing ever asks. Every item is optional, so this WARNs only when a
        # machine states nothing beyond which vault to use — a setup that
        # stopped halfway. Otherwise it reports, and the actionable moments
        # (migration, install) print the same list.
        from .machine import machine_setup

        missing = machine_setup.unstated(cfg.data)
        if not missing:
            rep.add("A8-machine-setup", "PASS",
                    "this machine has stated all of: "
                    + ", ".join(machine_setup.stated(cfg.data)))
            return
        names = ", ".join(item["key"] for item in missing)
        if not machine_setup.stated(cfg.data):
            rep.add("A8-machine-setup", "WARN",
                    f"this machine has stated nothing beyond its vault — "
                    f"unstated: {names}",
                    fix="; ".join(item["how"] for item in missing))
            return
        rep.add("A8-machine-setup", "PASS",
                f"stated: {', '.join(machine_setup.stated(cfg.data))}; "
                f"unstated (optional): {names}")

    _guard(rep, "A1", a1)
    _guard(rep, "A2", a2)
    _guard(rep, "A7", a7)
    _guard(rep, "A8", a8)
    _guard(rep, "A6", lambda: config_paths_block(rep, cfg))
    _guard(rep, "A3", a3)
    _guard(rep, "A4", a4)
    _guard(rep, "A5", a5)

    # ---- Block B: semantic search core --------------------------------
    def b2():
        missing = []
        for mod in ("numpy", "yaml", "watchdog", "psutil"):
            try:
                importlib.import_module(mod)
            except ImportError:
                missing.append(mod)
        try:
            importlib.import_module("sentence_transformers")
        except ImportError:
            missing.append("sentence-transformers")
        if missing:
            rep.add("B2-deps", "FAIL", f"missing: {', '.join(missing)}",
                    fix="pip install -r requirements.txt")
        else:
            rep.add("B2-deps", "PASS", "python deps importable")

    def b3():
        from .search import client, model_store

        state = model_store.status(cfg)
        if state["installed"]:
            rep.add("B3-model", "PASS",
                    f"{state['model']} @ {state.get('revision', '?')} "
                    f"({state.get('snapshot', '?')})")
            return
        if fix and heavy:
            installed = model_store.install(cfg)
            client.stop_daemon(cfg)
            daemon_ok = client.start_daemon(cfg)
            rep.add("B3-model", "FIXED" if daemon_ok else "FAIL",
                    f"{installed.get('source', 'installed')}; "
                    f"daemon={'restarted' if daemon_ok else 'restart failed'}",
                    fix="" if daemon_ok else "mb daemon start")
            return
        rep.add("B3-model", "FAIL",
                f"local embedding model unavailable: {state.get('reason', 'unknown')}; "
                f"cache={state.get('cache', cfg.hf_cache)}",
                fix="mb model install  (or mb doctor --fix --heavy)")

    def b4():
        from .search import indexer

        files = indexer.index_files(cfg.memory_index_dir)
        if files is not None:
            emb, meta = files
            data = json.loads(meta.read_text(encoding="utf-8"))
            chunks = data.get("chunks", [])
            n = len(chunks) if isinstance(chunks, list) else 0
            if n > 0:
                age_h = (time.time() - emb.stat().st_mtime) / 3600
                status = "PASS" if age_h < 48 else "WARN"
                rep.add("B4-index", status,
                        f"{n} chunks, model={data.get('model','?')}, age={age_h:.0f}h")
                return
        if fix and heavy:
            from .search import client
            stats = client.run_index(cfg, "rebuild")
            rep.add("B4-index", "FIXED", f"rebuilt: {stats}")
        else:
            rep.add("B4-index", "FAIL", "memory index missing/empty",
                    fix="mb index rebuild  (or mb doctor --fix --heavy)")

    def b6b_sessions():
        """The opt-in session store: size and reachability.

        Off is a PASS, not a warning — the whole point is that it is a choice.
        What needs saying out loud is size: transcripts run to tens of MB, and a
        store that grows silently is discovered last."""
        from .telemetry import sessions
        snap = sessions.stats(cfg)
        if not snap["enabled"]:
            rep.add("B6b-sessions", "PASS", "off (session_search_enabled=false)")
            return
        limit = float(cfg.get("session_search_max_mb") or 200)
        detail = (f"{snap['messages']} messages, {snap['size_mb']} MB, "
                  f"clients: {', '.join(snap['covered_clients'])}")
        if snap["size_mb"] > limit:
            rep.add("B6b-sessions", "WARN", f"{detail} — over {limit} MB",
                    fix="mb sessions purge  (or lower session_search_retention_days)")
        else:
            rep.add("B6b-sessions", "PASS", detail)

    def b7():
        from .machine.code_version import is_stale
        from .search import client
        pong = client.ping(cfg)
        if pong:
            # A daemon that answers is not the same as a daemon that answers
            # with the code on disk: `reload` re-reads config and indexes, never
            # modules. Left unreported, an upgraded engine keeps serving the old
            # logic and every measurement taken against it is quietly about the
            # previous build (machine/code_version.py records the case).
            if is_stale(pong.get("code_fingerprint")):
                rep.add("B7-daemon", "WARN",
                        f"running (pid={pong.get('pid')}) but serving code older "
                        f"than the files on disk",
                        fix="mb daemon restart  (reload does not re-read modules)")
            else:
                rep.add("B7-daemon", "PASS", f"ping ok, pid={pong.get('pid')}")
        elif fix:
            ok = client.start_daemon(cfg)
            rep.add("B7-daemon", "FIXED" if ok else "FAIL",
                    "started" if ok else "start failed — see .mb/daemon.log")
        else:
            rep.add("B7-daemon", "WARN", "daemon not running",
                    fix="mb daemon start")

    def b8():
        from .search import client
        if not client.ping(cfg):
            rep.add("B8-smoke", "SKIP", "daemon down")
            return
        resp = client.search(cfg, {"cmd": "search", "query": "memory brain",
                                   "top_k": 1, "format": "json"})
        n = len(resp.get("results", []))
        rep.add("B8-smoke", "PASS" if n else "WARN",
                f"'memory brain' -> {n} hits, {resp.get('time_ms','?')}ms")

    def b9():
        from .search import client
        st = client.status(cfg)
        if st.get("orphan"):
            if fix:
                client._clear_state(cfg)
                rep.add("B9-daemon-identity", "FIXED",
                        "orphan daemon state (process gone) cleaned")
            else:
                rep.add("B9-daemon-identity", "WARN",
                        f"orphan daemon state: pid {st.get('pid')} not alive",
                        fix="mb doctor --fix")
        else:
            rep.add("B9-daemon-identity", "PASS",
                    f"no orphan (instance={str(st.get('instance_id'))[:8] or '-'})")

    _guard(rep, "B2", b2)
    _guard(rep, "B3", b3)
    _guard(rep, "B4", b4)
    _guard(rep, "B6b", b6b_sessions)
    _guard(rep, "B7", b7)
    _guard(rep, "B8", b8)
    _guard(rep, "B9", b9)

    # ---- Block C: scheduler -------------------------------------------
    def c1():
        from .knowledge import nightly

        job = nightly.nightly_job(cfg)
        state = scheduler.verify(job)
        if not state.get("registered"):
            if fix:
                # migrate_scheduler_job also removes the legacy standalone
                # "nightly-index" job if still present (T12) — single source
                # shared with the installer, so the two paths can't drift.
                res = nightly.migrate_scheduler_job(cfg)
                rep.add("C1-nightly", "FIXED" if res.startswith("OK") else "FAIL", res)
            else:
                rep.add("C1-nightly", "WARN", "nightly job not registered",
                        fix="mb doctor --fix")
            return
        # Registered — but is the STORED action the argv we intended? A task
        # whose --vault path was split apart still shows "registered"; that must
        # not read as healthy.
        if not _action_matches(state, scheduler.expected_arguments(job)):
            if fix:
                res = nightly.migrate_scheduler_job(cfg)
                rep.add("C1-nightly", "FIXED" if res.startswith("OK") else "FAIL",
                        f"stale/broken action re-registered: {res}")
            else:
                rep.add("C1-nightly", "FAIL",
                        f"registered action != expected argv (stored: {state.get('arguments')!r})",
                        fix="mb doctor --fix")
            return
        status = "PASS" if state.get("coalescing") else "WARN"
        rep.add("C1-nightly", status,
                f"registered + action verified ({current_os()}), "
                f"coalescing={state.get('coalescing')}")

    def c2():
        from .machine import watch as watch_mod

        # The login task must be registered AND its STORED action must be the
        # argv we intended — a watch task whose --vault path lost its quoting
        # (pre-AUD-01 registration) exits 1 at every login and is otherwise
        # visible only as "no heartbeat" (incident 2026-07-20).
        job = watch_mod.watch_job(cfg)
        state = scheduler.verify(job)
        problem = None
        if not state.get("registered"):
            problem = ("WARN", "watch login task not registered")
        elif not _action_matches(state, scheduler.expected_arguments(job)):
            problem = ("FAIL",
                       f"registered action != expected argv (stored: {state.get('arguments')!r})")
        if problem:
            if fix:
                res = scheduler.register(job)
                rep.add("C2-watcher", "FIXED" if res.startswith("OK") else "FAIL",
                        f"{problem[1]} -> re-registered: {res} "
                        "(starts at next login; `mb watch` to start now)")
            else:
                rep.add("C2-watcher", problem[0], problem[1], fix="mb doctor --fix")
            return
        # Registration verified — now liveness via the heartbeat file.
        hb = cfg.watch_heartbeat_file
        if not hb.exists():
            rep.add("C2-watcher", "WARN", "no watcher heartbeat (watcher not running?)",
                    fix="mb watch  (or check login task)")
            return
        try:
            age = time.time() - float(hb.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            rep.add("C2-watcher", "WARN", "unreadable watcher heartbeat")
            return
        if age <= WATCH_HEARTBEAT_MAX_AGE_S:
            rep.add("C2-watcher", "PASS", f"watcher heartbeat {age:.0f}s ago")
        else:
            rep.add("C2-watcher", "WARN",
                    f"watcher heartbeat stale ({age/60:.0f}min)", fix="restart mb watch")

    def c5():
        if cfg.lock_dir.exists():
            age_h = (time.time() - cfg.lock_dir.stat().st_mtime) / 3600
            if age_h > 6:
                if fix:
                    shutil.rmtree(cfg.lock_dir, ignore_errors=True)
                    rep.add("C5-lock", "FIXED", f"stale lock ({age_h:.0f}h) removed")
                else:
                    rep.add("C5-lock", "WARN", f"stale lock ({age_h:.0f}h)")
            else:
                rep.add("C5-lock", "WARN", "rebuild in progress?")
        else:
            rep.add("C5-lock", "PASS", "no stale lock")

    _guard(rep, "C1", c1)
    _guard(rep, "C2", c2)
    _guard(rep, "C5", c5)

    # ---- Block D: host tools ------------------------------------------
    def d4():
        # Presence only. Whether git can actually produce a commit is D8 —
        # necessary and sufficient are different questions (issue #11).
        missing = [t for t in ("git",) if shutil.which(t) is None]
        if missing:
            rep.add("D4-cli", "FAIL", f"missing on PATH: {missing}")
        else:
            rep.add("D4-cli", "PASS", "git on PATH")

    def d8():
        # issue #11: D4 answers "is the binary present", which is a weaker
        # question than the one that matters — "can this machine produce a
        # commit". A new machine has no user.name/user.email until someone sets
        # them, and this project exists to get a new machine productive from an
        # existing vault; several skills and agent workflows here run `git add`
        # / `git commit` inside it. The failure otherwise lands mid-session,
        # dressed up as a Singularity problem.
        git = shutil.which("git")
        if git is None:
            rep.add("D8-git-identity", "SKIP", "git not on PATH (see D4-cli)")
            return
        inside = subprocess.run([git, "-C", str(cfg.vault), "rev-parse",
                                 "--is-inside-work-tree"],
                                capture_output=True, text=True, check=False)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            # A vault that is not under git needs no commit identity; warning
            # about it would be noise, not diagnosis.
            rep.add("D8-git-identity", "SKIP", "vault is not a git work tree")
            return
        missing = []
        for key in ("user.name", "user.email"):
            # run inside the vault so a deliberate per-repo identity counts:
            # `--get` already resolves local over global, and checking --global
            # alone would warn at people who configured this on purpose
            out = subprocess.run([git, "-C", str(cfg.vault), "config", "--get", key],
                                 capture_output=True, text=True, check=False)
            if not out.stdout.strip():
                missing.append(key)
        if not missing:
            rep.add("D8-git-identity", "PASS", "git can commit into the vault")
        else:
            # WARN, never FAIL, and NEVER auto-fixed even with --fix: a commit
            # identity is a personal choice that ends up permanently embedded
            # in published history. Guessing it from $USER/hostname or from the
            # GitHub CLI would be wrong.
            rep.add("D8-git-identity", "WARN",
                    f"git cannot commit into the vault: {', '.join(missing)} unset — "
                    f"the first vault commit will fail with 'Author identity unknown'",
                    fix='git config --global user.name "Your Name" && '
                        'git config --global user.email "you@example.com"')

    def d10():
        # The inventory the registry's docstring described in prose: "72 rule
        # notes, minus what DELIVERIES covers, minus the out-of-scope groups, is
        # zero". Nothing ever computed it, which is how a rule sat in none of the
        # three buckets for four days without anything saying so.
        #
        # It lives HERE and not in the test suite on purpose. Reconciling needs
        # the vault, and the engine may not make its behaviour depend on which
        # vault is configured; the doctor is the surface that is allowed to look
        # at one and degrade to SKIP when there is none. The classifying logic is
        # a pure function over a directory, unit-tested against a fixture, so the
        # check cannot quietly stop checking when this machine has no vault.
        from .machine import rules_delivery

        # `memory_folder`, never a hand-joined path: the layout of a vault is
        # known in config and nowhere else. `test_architecture` scans for the
        # joined form, so this comment must not spell it either.
        rules_dir = cfg.memory_folder("rules") if cfg.vault else None
        if rules_dir is None or not rules_dir.is_dir():
            rep.add("D10-rules-inventory", "SKIP", "vault has no rules/ directory")
            return
        orphans = rules_delivery.unaccounted_rules(rules_dir)
        ceiling = rules_delivery.UNACCOUNTED_CEILING
        total = len(list(rules_dir.glob("*.md")))
        if len(orphans) > ceiling:
            fresh = orphans[:3]
            rep.add("D10-rules-inventory", "WARN",
                    f"{len(orphans)} of {total} rule notes are delivered nowhere and "
                    f"recorded as out of scope nowhere (was {ceiling}): "
                    f"{', '.join(fresh)}"
                    + (" …" if len(orphans) > 3 else ""),
                    fix="graft the rule and add a '## Where it is grafted' section "
                        "to the note, or record it in NOT_OURS_TO_DELIVER with a reason")
        elif len(orphans) < ceiling:
            rep.add("D10-rules-inventory", "PASS",
                    f"{len(orphans)} of {total} unaccounted — below the recorded "
                    f"{ceiling}; lower UNACCOUNTED_CEILING to keep the ratchet tight")
        else:
            rep.add("D10-rules-inventory", "PASS",
                    f"{len(orphans)} of {total} unaccounted, unchanged")

    def d2():
        # every engine-owned agent (singularity/agents/*.md) must be registered
        # in ~/.claude/agents so councils can spawn them by portable name.
        src_dir = Path(__file__).resolve().parent / "agents"
        owned = sorted(p.stem for p in src_dir.glob("*.md")) if src_dir.is_dir() else []
        if not owned:
            rep.add("D2-agents", "SKIP", "no engine agents/ dir")
            return
        dest = Path.home() / ".claude" / "agents"
        missing = [n for n in owned if not (dest / f"{n}.md").exists()]
        if not missing:
            rep.add("D2-agents", "PASS", f"{len(owned)} engine agents registered")
        else:
            rep.add("D2-agents", "WARN", f"agents not registered: {', '.join(missing)}",
                    fix="python install/register_agents.py")

    def d1():
        # T02: desired/installed/effective capture-hooks state across every
        # client, not just a crude "singularity" substring check on Claude.
        # An intentionally disabled capture toggle is a deliberate user choice,
        # never a failure — only a missing registration is worth a WARN.
        from .machine import hooks_state

        state = hooks_state.status(cfg.vault)
        if state["source"] == "error":
            rep.add("D1-hooks", "WARN",
                    f"capture hooks config unreadable, treated as default: "
                    f"{state['warnings'][0] if state['warnings'] else '?'}",
                    fix="fix or remove ~/.singularity.json, then mb hooks status")
            return
        desired = "enabled" if state["configured"] else "disabled"
        clients = state["clients"]
        installed_any = any(c["installed"] for c in clients.values())
        detail = (f"capture hooks {desired} (default "
                  f"{'enabled' if state['default'] else 'disabled'}); " +
                  ", ".join(f"{n}={'installed' if c['installed'] else 'not-installed'}"
                           for n, c in clients.items()))
        if not installed_any:
            rep.add("D1-hooks", "WARN", detail + " — no client has the hook registered",
                    fix="python install/register_hooks.py")
        elif not state["configured"]:
            rep.add("D1-hooks", "PASS", detail + " — intentionally disabled, not a failure")
        else:
            rep.add("D1-hooks", "PASS", detail)

    def d3():
        # T07: module-context PreToolUse hook registration (Claude only, v1).
        settings = Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            rep.add("D3-module-context", "SKIP", "no ~/.claude/settings.json")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            rep.add("D3-module-context", "WARN", "settings.json unparseable")
            return
        pre = data.get("hooks", {}).get("PreToolUse", []) if isinstance(data, dict) else []
        installed = False
        if isinstance(pre, list):
            for entry in pre:
                if not isinstance(entry, dict):
                    continue
                if entry.get("_mb"):
                    installed = True
                    break
                for h in entry.get("hooks", []):
                    if isinstance(h, dict) and "module_context.py" in str(h.get("command", "")):
                        installed = True
                        break
        from .config import read_config_tolerant
        enabled = bool(read_config_tolerant().get("module_context_enabled", False))
        state = "enabled" if enabled else "disabled (default, opt-in via module_context_enabled)"
        if installed:
            rep.add("D3-module-context", "PASS", f"PreToolUse hook registered, {state}")
        else:
            rep.add("D3-module-context", "WARN", f"module-context hook not registered, {state}",
                    fix="python install/register_hooks.py")

    def d5():
        # T08: vault-guard PreToolUse hook registration (Claude only, v1).
        settings = Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            rep.add("D5-vault-guard", "SKIP", "no ~/.claude/settings.json")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            rep.add("D5-vault-guard", "WARN", "settings.json unparseable")
            return
        pre = data.get("hooks", {}).get("PreToolUse", []) if isinstance(data, dict) else []
        installed = False
        if isinstance(pre, list):
            for entry in pre:
                if not isinstance(entry, dict):
                    continue
                if entry.get("_mb"):
                    installed = True
                    break
                for h in entry.get("hooks", []):
                    if isinstance(h, dict) and "vault_guard.py" in str(h.get("command", "")):
                        installed = True
                        break
        from .config import DEFAULT_CONFIG, read_config_tolerant
        light_cfg = read_config_tolerant()          # per machine since 0.7
        enabled = bool(light_cfg.get("vault_guard_enabled",
                                     DEFAULT_CONFIG["vault_guard_enabled"]))
        mode = light_cfg.get("vault_guard_mode", DEFAULT_CONFIG["vault_guard_mode"])
        # The guard is on by default now, so "disabled" is somebody's decision,
        # not the shipped state — say which key expresses it rather than calling
        # it the default.
        state = f"mode={mode}" if enabled else "disabled (vault_guard_enabled: false)"
        if installed:
            rep.add("D5-vault-guard", "PASS", f"PreToolUse hook registered, {state}")
        else:
            rep.add("D5-vault-guard", "WARN", f"vault-guard hook not registered, {state}",
                    fix="python install/register_hooks.py")

    def d9():
        # The task router (UserPromptSubmit). Every other Claude-side hook the
        # engine registers is observable here — D1 capture, D3 module-context,
        # D5 vault-guard — and without an entry a full PASS run says nothing
        # about whether the thing that announces the chain of work is installed.
        settings = Path.home() / ".claude" / "settings.json"
        if not settings.exists():
            rep.add("D9-route-hook", "SKIP", "no ~/.claude/settings.json")
            return
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError:
            rep.add("D9-route-hook", "WARN", "settings.json unparseable")
            return
        ups = data.get("hooks", {}).get("UserPromptSubmit", []) if isinstance(data, dict) else []
        installed = False
        if isinstance(ups, list):
            for entry in ups:
                if not isinstance(entry, dict):
                    continue
                if entry.get("_mb"):
                    installed = True
                    break
                # pre-marker tolerance, same contract as D1/D3
                for h in entry.get("hooks", []):
                    if isinstance(h, dict) and "user_prompt_submit.py" in str(h.get("command", "")):
                        installed = True
                        break
        if installed:
            rep.add("D9-route-hook", "PASS",
                    "UserPromptSubmit hook registered (announces the chain of work)")
        else:
            rep.add("D9-route-hook", "WARN", "task-router hook not registered — "
                    "no chain is announced when a task arrives",
                    fix="python install/register_hooks.py")

    def d6():
        # incident 2026-07-14 "mb POSIX shim silently dies in Git Bash": the
        # extensionless bin/mb shadows mb.cmd in Git Bash, so it must actually
        # run there — exit 0 with non-empty output, never a silent 127.
        shim = Path(__file__).resolve().parents[1] / "bin" / "mb"
        if not shim.exists():
            rep.add("D6-posix-shim", "SKIP", "no bin/mb POSIX shim")
            return
        sh = shutil.which("sh") or shutil.which("bash")
        if sh is None:
            rep.add("D6-posix-shim", "SKIP", "no sh/bash on PATH (POSIX shim unused)")
            return
        # Two CWDs, always, in a temp tree we control. Running only from
        # doctor's own directory made the verdict depend on where the user
        # stood (issue #16): `python -m singularity` puts the CWD on sys.path,
        # so a CWD that contains a `singularity/` folder — the parent of a
        # standard clone — shadows the package with a namespace package and
        # the shim dies, while the same command from anywhere else passes.
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            neutral = Path(td) / "neutral"
            decoy = Path(td) / "decoy"
            neutral.mkdir()
            (decoy / "singularity").mkdir(parents=True)
            out = ""
            for label, cwd in (("a neutral CWD", neutral),
                               ("a CWD containing singularity/", decoy)):
                code, out, err = _run_shim(sh, shim, cwd=str(cwd))
                if code != 0 or not out:
                    detail = (f"`sh bin/mb --version` from {label}: exit {code}, "
                              f"stdout {'empty' if not out else repr(out)}")
                    if err:
                        detail += f", stderr: {err[:160]}"
                    rep.add("D6-posix-shim", "FAIL", detail,
                            fix="bin/mb must exec the venv console script or "
                                "bin/_launch.py — never `python -m`, which puts "
                                "the CWD on sys.path")
                    return
        rep.add("D6-posix-shim", "PASS",
                f"`sh bin/mb --version` -> {out} (neutral CWD and a CWD "
                f"containing singularity/)")

    def d7():
        # issue #15: `pip install -e` generates the console scripts into
        # <engine>/.venv/bin, which nothing puts on PATH — so every documented
        # `mb …` command failed on a machine whose doctor summary was clean.
        # Presence is not enough here either: `mb` resolving to ANOTHER clone's
        # venv is its own quiet disaster (two engines, one vault).
        rp = _load_install("register_path")
        st = rp.status()
        if st["is_ours"]:
            # "ours" is about the ENGINE the command runs, not the directory
            # its launcher sits in: an editable install into a system Python
            # lands the script outside the engine and imports it all the same.
            via = ("" if st.get("in_engine_dir") else
                   f" (another interpreter, same engine: {st['imports_engine']})")
            rep.add("D7-cli-on-path", "PASS", f"mb -> {st['resolved']}{via}")
        elif st["on_path"] and st.get("imports_engine"):
            rep.add("D7-cli-on-path", "WARN",
                    f"`mb` resolves to {st['resolved']}, which runs a DIFFERENT "
                    f"engine ({st['imports_engine']}), not this one "
                    f"({st['engine']}) — two engines on one vault can disagree "
                    f"about config, model and daemon state",
                    fix="python install/register_path.py, then put "
                        "the engine venv ahead of it on PATH (or remove the "
                        "other install)")
        elif st["on_path"]:
            rep.add("D7-cli-on-path", "WARN",
                    f"`mb` resolves to {st['resolved']} and it could not be "
                    f"determined which engine that runs — this engine's venv "
                    f"is {st['venv_bin']}",
                    fix="run `mb doctor` through that command and compare the "
                        "reported engine, or python install/register_path.py "
                        "to put this engine's venv on PATH")
        else:
            rep.add("D7-cli-on-path", "WARN",
                    "`mb` is not callable by name — every documented `mb …` "
                    "command fails in a fresh shell",
                    fix="python install/register_path.py")

    _guard(rep, "D4", d4)
    _guard(rep, "D8", d8)
    _guard(rep, "D7", d7)
    _guard(rep, "D2", d2)
    _guard(rep, "D1", d1)
    _guard(rep, "D3", d3)
    _guard(rep, "D5", d5)
    _guard(rep, "D6", d6)
    _guard(rep, "D9", d9)
    _guard(rep, "D10", d10)

    # ---- Block E: Gemini CLI integration (GCLI-05) --------------------
    _guard(rep, "E-gemini", lambda: gemini_block(rep, fix))

    # ---- Block F: Grok Build CLI integration (GRK-03/04) --------------
    _guard(rep, "F-grok", lambda: grok_block(rep, fix))

    # ---- Block W: WIKI-LOOP ingest layer (W11) ------------------------
    _guard(rep, "W-wiki-loop", lambda: wiki_loop_block(rep, cfg, fix))

    # ---- Block K: CODE-INDEX corpus (C05) -----------------------------
    _guard(rep, "K-code-index", lambda: code_index_block(rep, cfg, fix, heavy))

    # ---- Block K2: CODE-GRAPH, the structural graph (P6) ----------------
    _guard(rep, "K2-code-graph", lambda: code_graph_block(rep, cfg))

    # ---- summary -------------------------------------------------------
    counts = rep.counts()
    payload = {"counts": counts, "checks": rep.checks, "ok": not rep.has_fail}
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    (cfg.mb_dir / "doctor-last.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    if as_json:
        # exactly one machine-readable document on stdout, nothing else
        print(json.dumps(payload, ensure_ascii=False))
        return 1 if rep.has_fail else 0

    print("\n================ mb doctor summary ================")
    print(" | ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    # WARNs are repeated here, not just inline above (issue #10, point 4): a
    # phase that skipped a requested feature must not be indistinguishable from
    # one that succeeded when the exit code is 0, and the summary is the part
    # people actually read after a few hundred lines of install output.
    for status in ("FAIL", "WARN"):
        for c in rep.checks:
            if c["status"] == status:
                print(f"  {status} {c['id']}: {c['detail']}"
                      + (f"  fix: {c['fix']}" if c["fix"] else ""))
    print("===================================================")
    return 1 if rep.has_fail else 0
