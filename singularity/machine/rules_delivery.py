"""Where each rule is DELIVERED — the registry, and the check that it is still there.

A rule note in the vault records why a rule exists: which incident produced it,
what it cost, who decided. That is knowledge, and it belongs in the vault.

It is not, however, a mechanism. Six defects in a single day (v0.6.1 -> v0.7.1)
had the same anatomy: the rule was written down and absent at the moment the
action was chosen. So every rule that must ACT names a delivery point in the
engine — a SessionStart fragment, the shared agent preamble, one agent file, a
skill — and this module records the pairing so that the injection cannot quietly
disappear in the next refactor. `check()` returns what is missing; the test
suite fails on a non-empty result.

Two invariants this file exists to protect:

1. Delivery points live in the ENGINE, never in the vault. Swapping, emptying or
   losing the vault must cost recall — never the machine's behaviour. Anything a
   subagent or the main thread is REQUIRED to follow ships with the code.
2. One source, injected. A marker is quoted from the constant that carries it
   (or from the single file that owns it), so a second copy cannot drift.
"""
from __future__ import annotations

from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
SKILLS = ENGINE / "singularity" / "skills"
AGENTS = ENGINE / "singularity" / "agents"

PREAMBLE = AGENTS / "AGENT_SHARED_PREAMBLE.md"

#: (rule, surface, path, marker) — `marker` is a distinctive phrase from the
#: injected text, short enough to survive editing and specific enough that it
#: cannot match anything else in the file.
DELIVERIES: list[tuple[str, str, Path, str]] = [
    # --- main thread: the SessionStart hook is its only mandatory surface -----
    ("memory means the vault", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "MEMORY_WRITES_FRAGMENT"),
    ("writes to the vault go through mb commands", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "mb note new <type>"),
    ("working AI artefacts are stored in the vault", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "second source of truth"),
    ("a rule is spoken at the point of decision", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "RULE_POINT_FRAGMENT"),
    ("check a found inconsistency against the recorded decision", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "look for a RECORDED decision"),
    # 2026-08-21, vibe24 T-166: the transport of a migration was built and reported as
    # "the new flow works", with four green loops behind it. The owner answered "old
    # flow" five times running. Both were right and the word meant two things: the
    # logs proved an envelope reached the end of new rails and said nothing about
    # there being ONE stage at the end of them. What broke the deadlock was a frame
    # strip of the finished reel, not a sixth log.
    ("measure the axis you were asked about", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "MEASURED_AXIS_FRAGMENT"),
    ("measure the axis you were asked about", "SessionStart",
     ENGINE / "singularity" / "hooks" / "session_start.py",
     "MEASURED_AXIS_FRAGMENT"),

    # --- every subagent: preamble sections 1-2 are the mandatory minimum -----
    ("a claim of absence requires a control", "preamble §2", PREAMBLE,
     "is not evidence by itself"),
    # The SAME rule, second mechanism, same line — and its own marker, because the
    # first one goes on matching after the second half is deleted. `accounted` is a
    # set, so this costs the inventory nothing and buys the half a check.
    # 2026-08-18, vibe24 V24-BUS-009: a complete, correct query returned 70 entries
    # under 70 keys, and that was read as "reuse is broken". It was 14 tenants with
    # one job each; reuse needs two jobs in a tenant, so the phenomenon had no
    # opportunity to occur. The old GATE passes on this — the code under test
    # existed and deleting it would have changed the numbers.
    ("a claim of absence requires a control", "preamble §2", PREAMBLE,
     "had no opportunity to exhibit it"),
    ("check a found inconsistency against the recorded decision", "preamble §2", PREAMBLE,
     "look for a recorded decision"),
    ("measure the axis you were asked about", "preamble §2", PREAMBLE,
     "Measure the axis you were ASKED about"),
    ("reality anchor - evidence over confidence", "preamble §2", PREAMBLE,
     "Reality anchor"),
    ("an invariant is proven by trying to break it", "preamble §2", PREAMBLE,
     "plant the violation"),
    ("a pipe over the test command must not swallow the exit code", "preamble §2", PREAMBLE,
     "PIPESTATUS"),
    ("re-check an evidential measurement when the population changes", "preamble §3", PREAMBLE,
     "together with the population"),
    ("when an agent re-renders its own input, the input is what to fix", "preamble §6", PREAMBLE,
     "defect of the INPUT"),
    ("a function covered by tests needs a consumer", "preamble §7", PREAMBLE,
     "composition root"),
    ("a recoverable error hides a total failure", "preamble §7", PREAMBLE,
     "recoverable / previous state stands"),
    ("a source scanner must not write what it searches for", "preamble §7", PREAMBLE,
     "no needle may occur literally"),
    ("a release against a class of defect gives a scanner for the class",
     "preamble §7", PREAMBLE, "One instance of a defect is not the defect"),

    ("every changed line traces back to the request", "preamble §7", PREAMBLE,
     "traces back to the request"),
    ("minimum code, nothing speculative", "preamble §7", PREAMBLE,
     "over-engineered"),
    ("name the fork before the work, not after", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "The boundary is WHEN, not who"),

    # --- skills: the rule stands where that class of work is done ------------
    ("Playwright networkidle never arrives on pages with video", "skill",
     SKILLS / "visual-regression-bootstrap" / "SKILL.md", "networkidle"),
    ("byte-identical screenshots require --disable-gpu", "skill",
     SKILLS / "visual-regression-bootstrap" / "SKILL.md", "--disable-gpu"),
    ("python http.server does not serve Range", "skill",
     SKILLS / "visual-regression-bootstrap" / "SKILL.md", "does not serve Range"),
    ("canvas animation runs on wall-time only", "skill",
     SKILLS / "motion-3d-implementer" / "SKILL.md", "delta = time - lastTime"),
    ("img src svg does not inherit currentColor", "skill",
     SKILLS / "motion-3d-implementer" / "SKILL.md", "currentColor"),
    ("an anomaly in interactive Chrome is a lead", "skill",
     SKILLS / "design-fidelity-audit" / "SKILL.md", "Playwright is the only source of truth"),
    ("an anomaly in interactive Chrome is a lead", "agent",
     AGENTS / "web-viewer.md", "Playwright-only rule"),
    ("advisor answers only a justified question", "agent",
     AGENTS / "advisor.md", "ПОЧЕМУ FABLE:"),
    ("scout quotes, never paraphrases", "agent",
     AGENTS / "scout.md", "file:line"),
    ("environment claims are checked with a live probe", "skill",
     SKILLS / "planning-council" / "SKILL.md", "live probe"),
    ("agentic-development estimates have two cost classes", "skill",
     SKILLS / "planning-council" / "SKILL.md", "COST CLASS"),
    ("session end ... continuation prompt", "skill",
     SKILLS / "compaction-plan" / "SKILL.md", "continuation prompt"),
    ("lift knowledge into indexable notes before archiving", "skill",
     SKILLS / "memory-curator" / "SKILL.md", "Archiving is where knowledge is lost"),
    ("a rule is spoken at the point of decision", "skill",
     SKILLS / "mb-memory" / "SKILL.md", "in the engine, not in the vault"),
    ("read the library licence before recommending it", "skill",
     SKILLS / "pattern-source-scout" / "SKILL.md", "NOASSERTION"),
    ("read the library licence before recommending it", "skill",
     SKILLS / "component-intake" / "SKILL.md", "NOASSERTION"),
    ("read the library licence before recommending it", "skill",
     SKILLS / "website-factory" / "SKILL.md", "NOASSERTION"),
    ("the asset manifest is generated by walking the tree", "skill",
     SKILLS / "website-factory" / "SKILL.md", "coverage self-check"),
    ("a capability lives in the engine, a skill only routes to it", "skill",
     SKILLS / "website-factory" / "SKILL.md", "A capability lives in the engine"),
]

# Rules whose delivery point no longer exists. Kept visible on purpose: an entry
# silently dropped from DELIVERIES looks exactly like a rule that was never
# delivered, and the difference matters when the tool comes back.
#
# «A short envelope in ffmpeg is laid down with aeval, not volume eval=frame»
# stood in `reel-editor/SKILL.md` and in `reel/render.py::shutter_wav`'s docstring.
# v0.12.0 deleted the reel tool with the skill that pointed at it, so both
# surfaces are gone at once. The rule remains true about ffmpeg and remains in
# the vault as knowledge; it has no execution point in this engine until
# something here synthesises audio again. Inventing a home for it in a
# neighbouring skill would be rule-theatre — nobody writing an envelope would
# read it there.
WITHOUT_A_POINT: list[tuple[str, str]] = [
    ("a short envelope in ffmpeg is laid down with aeval",
     "reel-editor + reel/render.py were deleted in v0.12.0; the engine has no "
     "replacement that synthesises audio"),
]

# Rules that are NOT this engine's to deliver. Recorded so the inventory adds
# up: 72 rule notes, minus what DELIVERIES covers, minus these, is zero — and
# an untouched rule cannot hide behind "probably handled somewhere".
#
# The distinction that matters: none of these is a gap in Singularity. They are
# delivered by another repository, dormant on this platform, or already carried
# by the code they are about.
NOT_OURS_TO_DELIVER: list[tuple[str, str]] = [
    ("rules of the singularity-ui project (6)",
     "their point is in that repository (code standards, adapters, the "
     "approval/egress model); the Singularity engine does not execute them"),
    ("video-engine rules (4): cuts on positions, curve overshoot, a threshold "
     "out of a float, «deliberately not exported»",
     "the point is in the repository that owns that code; after reel was deleted "
     "in v0.12.0 this engine holds no video code"),
    ("Windows specifics (6): PowerShell round-trip, CRLF, quoting in git -m, "
     "argv through ANSI, check.ps1, both names of the shell tool",
     "dormant on a macOS machine; the rule needs a platform trigger, not a slot "
     "in a prompt that is paid for every session"),
    ("engine code invariants (11): hooks never call load_config, index readers "
     "go through index_files, the mkdir lock, the POSIX shim, utf-8-sig, "
     "ownership detection and others",
     "already delivered where that code is written: a docstring next to the "
     "implementation plus a test that fails on violation"),
]


#: A rule note declares its own delivery point in a section of this name. The
#: convention is not new — `docs/rules-delivery.md` step 6 prescribes it and 27
#: of the vault's notes already carry it. What was missing is anything that
#: NOTICES when a note carries neither this nor a mention in the engine.
DELIVERY_SECTION_MARKERS = ("## Где вживлено", "## Where it is grafted")

#: Unaccounted notes on 2026-08-08, measured. The number may fall; it may not
#: rise. It is a RATCHET rather than a target because the 43 are mostly the
#: groups `NOT_OURS_TO_DELIVER` describes in prose — other repositories, dormant
#: Windows specifics, code invariants delivered by a docstring plus a test — and
#: turning that prose into 43 names is work this ceiling makes visible instead of
#: pretending is done. What it buys immediately is the case it was written for:
#: a NEW rule note that nothing delivers moves the count up and is named.
UNACCOUNTED_CEILING = 43


def _engine_text() -> str:
    """Every word the engine ships, for the "is this note named anywhere" test."""
    parts = []
    for path in sorted((ENGINE / "singularity").rglob("*")):
        if path.is_file() and path.suffix in {".py", ".md"}:
            try:
                parts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(parts)


def unaccounted_rules(rules_dir: Path, engine_text: str | None = None) -> list[str]:
    """Rule notes that neither declare a delivery point nor are named in the engine.

    Two objective signals, no hand-maintained list to drift: the note says where
    it is grafted, or the engine mentions it by name (the Core Disciplines reach
    every agent as `[[wikilinks]]` from the preamble). A note with neither is not
    necessarily a defect — it may belong to another repository — but nobody has
    said so, and "untouched" and "covered somewhere else" must not look alike.

    Takes the directory rather than a `Config`: the engine may not depend on
    which vault is configured, so the caller supplies the path and this stays a
    pure function a fixture can exercise without one.
    """
    if not rules_dir.is_dir():
        return []
    text = _engine_text() if engine_text is None else engine_text
    orphans = []
    for note in sorted(rules_dir.glob("*.md")):
        try:
            body = note.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(marker in body for marker in DELIVERY_SECTION_MARKERS):
            continue
        if note.stem in text:
            continue
        orphans.append(note.stem)
    return orphans


def check() -> list[str]:
    """Missing deliveries, as human-readable lines. Empty list = all present."""
    missing: list[str] = []
    for rule, surface, path, marker in DELIVERIES:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            missing.append(f"{rule} -> {surface}: cannot read {path} ({exc})")
            continue
        if marker not in text:
            missing.append(
                f"{rule} -> {surface}: marker {marker!r} gone from "
                f"{path.relative_to(ENGINE)}")
    return missing


def vault_dependencies() -> list[str]:
    """Agent sources that still address the shared preamble through the vault.

    The preamble is the rule set every subagent starts on. Reading it out of
    `{{VAULT}}` made eight agents silently rule-less on any machine whose vault
    moved, was empty, or was swapped for another one."""
    offenders = []
    for src in sorted(AGENTS.glob("*.md")):
        if "{{VAULT}}/rules/AGENT_SHARED_PREAMBLE" in src.read_text(encoding="utf-8"):
            offenders.append(src.name)
    return offenders
