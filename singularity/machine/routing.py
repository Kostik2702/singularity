"""Task class -> chain of work, and the marker-based classifier that picks one.

The delivery architecture had three push moments — SessionStart, PreToolUse,
Stop — and none of them is "a task arrived". SessionStart fires before the task
exists, so it cannot classify anything; PreToolUse fires after the action is
already chosen, so it can only comment. The workflow router therefore lived as
a skill, which is a PULL surface: it ran only when someone remembered it, and
across a full working day it was never pulled once.

A rule is a constraint and can wait in context — "plant the violation" is
evaluated whenever a test gets written, wherever its text happens to sit. A
CHAIN is a procedure: it has to be ENTERED, and entering happens exactly once,
in the first second of a task. Prose in SessionStart cannot enter anything. So
the chain moved here, behind `UserPromptSubmit` — the one event that fires at
that second.

Two properties this module exists to keep:

1. Classification is DETERMINISTIC and states its evidence. No model call, no
   daemon, no embedding — a chain announced without saying which words chose it
   is one more thing to take on faith, and the point of the change was to make
   the chain transparent rather than merely present.
2. Silence is the default. A router that fires on "спасибо" trains its reader
   to skip it, and then it is worse than absent. Below `MIN_PROMPT_CHARS`, or
   with no strong marker, `classify()` returns None and the hook emits nothing.

stdlib only, no config load: this is imported by a hook, and a hook that can
raise is a hook that can end a session ([[Rule - hooks must never use the full
validated Config load path]]).
"""
from __future__ import annotations

import re
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[2]
WORKFLOW = ENGINE / "singularity" / "workflow"

#: Background-task notifications and other system-generated turns arrive through
#: the same `UserPromptSubmit` event as a person's message. Observed live: a
#: review report was classified as "эксплуатация движка" off the words
#: `mb doctor` quoted inside it. A machine talking to itself is not a task.
SYSTEM_PROMPT_SIGNALS = ("[system notification", "<task-notification>",
                         "<local-command-caveat>", "stop hook feedback:",
                         "hook additional context:")

#: Below this a prompt is an acknowledgement or a one-word follow-up — never
#: the start of a chain. Deliberately low: real tasks are often terse ("обнови
#: волт из гита" is 19 chars), so the marker list does the discriminating and
#: this only filters "да" / "ок" / "спасибо". Was 14 and swallowed real
#: commands: «запусти ревью» is 13 characters and carries the marker `ревью`.
MIN_PROMPT_CHARS = 10

#: Stems, not whole words: Russian inflects, and `почин` covers "починить",
#: "почини", "починка" without a morphology dependency. Matched against a
#: lowercased prompt with ё folded to е.
CHAINS: dict[str, dict] = {
    "dev": {
        "title": "разработка / фикс кода",
        "entry": "phase-SB-step-back.md",
        "steps": ["SB Step-Back", "0 Выбор подхода", "1 Investigation", "1.5 Grill",
                  "2 Planning", "3 Implementation", "4 Review", "5 Verify",
                  "6 Commit", "7 Docs + Self-learn"],
        "note": "пропуск фазы легален (мелкая правка), но фиксируется [➖] с причиной",
        "markers": ("почин", "пофикс", "фикс", "испра", "рефактор", "имплемент",
                    "реализ", "допиш", "перепиш", "сломал", "падает", "traceback",
                    "не работает", "регресс", "баг", "refactor", "implement",
                    "напиши код", "внеси правк", "зарегистрир", "внедри",
                    "напиши тест", "напиши скрипт", "напиши хук"),
    },
    "research": {
        "title": "исследование / разбор",
        "entry": "phase-1-investigation.md",
        "steps": ["SB Step-Back", "1 Investigation", "1.5 Grill", "7 Self-learn"],
        "note": "вывод не репортится, пока не пережил попытку опровержения (preamble §3)",
        "markers": ("разберись", "расследу", "исследу", "выясни", "проанализир",
                    "root cause", "investigate", "найди причину", "изучи",
                    "глубокий анализ", "почему"),
    },
    "review": {
        "title": "ревью / аудит",
        "entry": "phase-4-review.md",
        "steps": ["4 Review", "5 Verify"],
        "note": "старый PASS после жалобы — улика, что приёмка слепа, а не аргумент защиты",
        "markers": ("ревью", "review", "проверь код", "аудит кода", "аудит изменени",
                    "проведи аудит", "готово к мерж",
                    "проверь измен", "оспорь", "покритикуй"),
    },
    "web": {
        "title": "веб / вёрстка",
        "entry": None,
        "steps": ["website-factory", "design-reference-scout", "design-analysis",
                  "реализация слайса", "visual-regression-bootstrap",
                  "design-fidelity-audit", "motion-qa"],
        "note": "источник истины — только Playwright; интерактивный Chrome даёт лид, не баг",
        "markers": ("сайт", "лендинг", "верстк", "секци", "адаптив",
                    "landing", "website", "css", "html", "слайс"),
    },
    "image": {
        "title": "изображения",
        "entry": None,
        "steps": ["create-image-prompt", "image-asset-qa", "bg-remove"],
        "note": "приёмка кадра — просмотром отрисованного, не числами плана",
        "markers": ("картинк", "изображени", "промпт для", "убрать фон", "аватар"),
    },
    "memory": {
        "title": "память / волт",
        "entry": None,
        "steps": ["mb search (дедуп перед записью)", "mb note new / mb learn",
                  "memory-curator"],
        "note": "новая заметка — только `mb note new`; прямая запись в managed-корень флагуется guard'ом",
        "markers": ("запомни", "в память", "заметк", "волт", "learnings",
                    "закоммить память", "в vault", "память"),
    },
    "ops": {
        "title": "эксплуатация движка",
        "entry": None,
        "steps": ["mb doctor", "mb status", "предъявить код возврата команды"],
        "note": "код возврата снимается с самой команды, не с пайпа",
        # Whole phrases, not stems: bare "установ" also matches "установщике",
        # which routed "добавь хук и зарегистрируй его в установщике" — a
        # writing task — to operations. A marker that fires inside an unrelated
        # word is a misroute the reader has to notice and undo every time.
        "markers": ("mb doctor", "доктор", "переустанов", "установи", "установк",
                    "mb hooks", "включи хук", "выключи хук", "статус хук",
                    "stop hooks", "capture hooks", "auto-capture",
                    "config set", "обнови singularity", "обнови волт",
                    "обнови память", "выпусти релиз"),
    },
}

#: Tie-break when two classes match the same number of markers. Earlier wins.
#: `ops` last: its markers are the ones most likely to appear incidentally in a
#: task that is really about writing code ("зарегистрируй хук в установщике").
PRIORITY = ["memory", "web", "image", "review", "research", "dev", "ops"]


def _normalise(prompt: str) -> str:
    return prompt.lower().replace("ё", "е")


#: A marker is a STEM, so it may be followed by any inflection — but it must
#: begin where a word begins. Bare `фикс` matching inside "зафиксируй" routed a
#: report to development; `установ` inside "установщике" routed a writing task
#: to operations. Prefix-anchoring kills that whole class at once, which is what
#: the class was owed ([[Rule - релиз против класса дефекта даёт сканер класса]]).
_WORD_START = r"(?<![0-9a-zа-я_])"


def _matches(marker: str, text: str) -> bool:
    return re.search(_WORD_START + re.escape(marker), text) is not None


def classify(prompt: str) -> tuple[str, list[str]] | None:
    """(chain key, the markers that chose it) — or None to stay silent.

    Returning the matched markers is not decoration: a chain that cannot say
    why it was picked is indistinguishable from a guess, and the reader has no
    way to correct a misroute except to ignore the router entirely."""
    if not isinstance(prompt, str) or len(prompt.strip()) < MIN_PROMPT_CHARS:
        return None
    text = _normalise(prompt)
    if any(sig in text for sig in SYSTEM_PROMPT_SIGNALS):
        return None
    hits = {key: [m for m in chain["markers"] if _matches(m, text)]
            for key, chain in CHAINS.items()}
    hits = {k: v for k, v in hits.items() if v}
    if not hits:
        return None
    # COUNT first, length second. Length as the primary key let one long
    # incidental match outvote any number of short exact ones: "почини баг:
    # mb doctor падает на пустом волте" scored `mb doctor` (9) over dev's
    # три маркера and sent an ordinary fix to "эксплуатация движка".
    best = max(hits, key=lambda k: (len(hits[k]), max(len(m) for m in hits[k]),
                                    -PRIORITY.index(k)))
    return best, sorted(hits[best], key=len, reverse=True)


def phase_file(name: str) -> Path | None:
    """Absolute path to a phase file in the ENGINE, or None if it is gone.

    Engine-resolved on purpose: these files used to live in the vault, which
    made a twelve-phase development flow a property of which vault happened to
    be configured — the same dependency v0.13.0 removed for the agent preamble.
    """
    p = WORKFLOW / name
    return p if p.is_file() else None


def resolve_phase(phase_id: str) -> Path | None:
    """`5` -> phase-5-verify.md, `SB` -> phase-SB-step-back.md, `1.5` -> 1-5.

    Preamble §7 tells every subagent that `mb route` gives the path to phase 5.
    It gave the chain's ENTRY phase only, so the instruction was false — and a
    wrong path is worse than the dead wikilink it replaced, because it reads
    as correct."""
    if not isinstance(phase_id, str) or not phase_id.strip():
        return None
    pid = phase_id.strip().replace(".", "-").upper()
    for p in sorted(WORKFLOW.glob("phase-*.md")):
        stem = p.stem[len("phase-"):]
        if stem.upper() == pid or stem.upper().startswith(pid + "-"):
            return p
    return None


def render(key: str, markers: list[str]) -> str:
    """The text injected alongside the prompt. Budgeted: this is paid on every
    classified prompt, so it states the chain and the entry point and stops."""
    chain = CHAINS[key]
    lines = [f"[mb route] класс задачи: {chain['title'].upper()} "
             f"(по маркерам: {', '.join(repr(m) for m in markers[:4])})",
             "цепь: " + " → ".join(chain["steps"])]
    if chain["entry"]:
        entry = phase_file(chain["entry"])
        if entry is not None:
            lines.append(f"начни с: {entry}")
    lines.append(chain["note"])
    lines.append("Объяви фазу одной строкой перед началом; каждая фаза "
                 "заканчивается записью в learnings/active/<TASK>.md. "
                 "Не тот класс — скажи об этом и работай по своему.")
    return "\n".join(lines)
