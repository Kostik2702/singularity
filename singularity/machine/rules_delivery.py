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
    ("память означает vault", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "MEMORY_WRITES_FRAGMENT"),
    ("записи в vault идут командами mb", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "mb note new <type>"),
    ("правило звучит в точке принятия решения", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "RULE_POINT_FRAGMENT"),
    ("найденное несоответствие сверить с записанным решением", "SessionStart",
     ENGINE / "singularity" / "machine" / "hooks_state.py",
     "look for a RECORDED decision"),

    # --- every subagent: preamble sections 1-2 are the mandatory minimum -----
    ("утверждение об отсутствии требует контроля", "preamble §2", PREAMBLE,
     "само по себе не evidence"),
    ("найденное несоответствие сверить с записанным решением", "preamble §2", PREAMBLE,
     "ищи записанное решение"),
    ("якорь реальности - улики вместо уверенности", "preamble §2", PREAMBLE,
     "Якорь реальности"),
    ("инвариант доказывается попыткой его сломать", "preamble §2", PREAMBLE,
     "посади нарушение"),
    ("пайп поверх тестовой команды не должен глотать exit-код", "preamble §2", PREAMBLE,
     "PIPESTATUS"),
    ("доказательный замер перепроверять при смене популяции", "preamble §3", PREAMBLE,
     "вместе с популяцией"),
    ("если агент перерендеривает свой вход, чинить надо вход", "preamble §6", PREAMBLE,
     "дефект ВХОДА"),
    ("у покрытой тестами функции должен быть потребитель", "preamble §7", PREAMBLE,
     "композиционного корня"),
    ("recoverable-ошибка скрывает тотальный отказ", "preamble §7", PREAMBLE,
     "recoverable / previous state stands"),
    ("сканер исходника не пишет то, что ищет", "preamble §7", PREAMBLE,
     "игла не должна встречаться"),

    # --- skills: the rule stands where that class of work is done ------------
    ("Playwright networkidle не наступает на страницах с video", "skill",
     SKILLS / "visual-regression-bootstrap" / "SKILL.md", "networkidle"),
    ("byte-identity скриншотов требует --disable-gpu", "skill",
     SKILLS / "visual-regression-bootstrap" / "SKILL.md", "--disable-gpu"),
    ("python http.server не отдаёт Range", "skill",
     SKILLS / "visual-regression-bootstrap" / "SKILL.md", "не отдаёт Range"),
    ("canvas-анимации только на wall-time", "skill",
     SKILLS / "motion-3d-implementer" / "SKILL.md", "delta = time - lastTime"),
    ("img src svg не наследует currentColor", "skill",
     SKILLS / "motion-3d-implementer" / "SKILL.md", "currentColor"),
    ("аномалия в интерактивном Chrome — лид", "skill",
     SKILLS / "design-fidelity-audit" / "SKILL.md", "Источник истины — только Playwright"),
    ("аномалия в интерактивном Chrome — лид", "agent",
     AGENTS / "web-viewer.md", "Playwright-only rule"),
    ("environment-утверждения проверять живым probe", "skill",
     SKILLS / "planning-council" / "SKILL.md", "live probe"),
    ("оценки агентной разработки - два класса затрат", "skill",
     SKILLS / "planning-council" / "SKILL.md", "COST CLASS"),
    ("session end ... continuation prompt", "skill",
     SKILLS / "compaction-plan" / "SKILL.md", "continuation-промпт"),
    ("перед архивацией вынести знание в индексируемые заметки", "skill",
     SKILLS / "memory-curator" / "SKILL.md", "Архивация — точка потери знания"),
    ("правило звучит в точке принятия решения", "skill",
     SKILLS / "mb-memory" / "SKILL.md", "в движке, не в vault"),
    ("лицензию библиотеки читать до рекомендации", "skill",
     SKILLS / "pattern-source-scout" / "SKILL.md", "NOASSERTION"),
    ("лицензию библиотеки читать до рекомендации", "skill",
     SKILLS / "component-intake" / "SKILL.md", "NOASSERTION"),
    ("лицензию библиотеки читать до рекомендации", "skill",
     SKILLS / "website-factory" / "SKILL.md", "NOASSERTION"),
    ("манифест ассетов генерируется обходом дерева", "skill",
     SKILLS / "website-factory" / "SKILL.md", "coverage self-check"),
    ("возможность живёт в движке, скил только маршрутизирует", "skill",
     SKILLS / "website-factory" / "SKILL.md", "Возможность живёт в движке"),
]

# Rules whose delivery point no longer exists. Kept visible on purpose: an entry
# silently dropped from DELIVERIES looks exactly like a rule that was never
# delivered, and the difference matters when the tool comes back.
#
# «Короткая огибающая в ffmpeg кладётся aeval, а не volume eval=frame» stood in
# `reel-editor/SKILL.md` and in the docstring of `reel/render.py::shutter_wav`.
# v0.12.0 deleted the reel tool with the skill that pointed at it, so both
# surfaces are gone at once. The rule remains true about ffmpeg and remains in
# the vault as knowledge; it has no execution point in this engine until
# something here synthesises audio again. Inventing a home for it in a
# neighbouring skill would be rule-theatre — nobody writing an envelope would
# read it there.
WITHOUT_A_POINT: list[tuple[str, str]] = [
    ("короткая огибающая в ffmpeg кладётся aeval",
     "reel-editor + reel/render.py удалены в v0.12.0; замены, синтезирующей звук, "
     "в движке нет"),
]


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
