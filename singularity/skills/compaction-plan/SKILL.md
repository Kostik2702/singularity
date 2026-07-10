---
name: compaction-plan
description: Save full session state to the vault before context clear/compaction so nothing is lost. Use when the user says cp, compaction plan, "сохрани состояние сессии", "before clear", or when context is nearly exhausted (~10% remaining) - proactively offer it.
---

# compaction-plan — сохранение состояния перед clear

Порт Genevra compaction-plan: полный план продолжения пишется в vault, новая сессия подхватывает его через SessionStart-hook (recall) и продолжает без потерь.

## Протокол

- [ ] 1. Собери состояние ТЕКУЩЕЙ сессии: активная задача и фаза workflow; что сделано (с file:line/путями); что осталось (конкретные следующие шаги, не «продолжить»); открытые решения/вопросы с owner; важные пути/команды/значения, добытые в сессии; незакоммиченные изменения (`git status --short`).
- [ ] 2. Запиши в `D:\obsidian\memory brain\compaction-plans\<YYYY-MM-DD-HHmm>-<task>.md` (frontmatter: type=reference, title, created, task, tags:[compaction]). Плотно, без пересказа диалога — только то, что нужно для продолжения.
- [ ] 3. Сообщи пользователю одной строкой: план сохранён, после clear новая сессия увидит его автоматически.

## На стороне возобновления (новая сессия)

- [ ] SessionStart-hook показывает pending compaction-plans. Если пользователь говорит продолжить: прочитай ПОЛНЫЙ план + активный learnings-док задачи → **удали файл плана** (одноразовый; иначе будет висеть вечно) → продолжай с первого незакрытого шага.
- [ ] Не удаляй план, если пользователь выбрал другую работу — оставь до явного решения.
