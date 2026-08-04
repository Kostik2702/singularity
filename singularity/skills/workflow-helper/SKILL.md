---
name: workflow-helper
description: Navigate the solo development workflow (Singularity port of Genevra 12-phase flow). Use when the user says fw, wh, next, "следующая фаза", "what phase", "follow workflow", "начни задачу по воркфлоу", or when starting any non-trivial development task. Router + lazy-load - loads ONLY the current phase file from the vault.
---

# workflow-helper — навигация по соло-воркфлоу

Воркфлоу живёт в vault: `{{VAULT}}\workflow\` (router + файлы фаз). Дисциплина lazy-load (правило C9): грузи ТОЛЬКО текущую фазу, не всё сразу.

## Протокол

- [ ] 1. Прочитай router: `{{VAULT}}\workflow\_router.md` (карта фаз + сквозные правила). Если уже читал в этой сессии — не перечитывай (cache).
- [ ] 2. Определи текущую фазу:
    - новая задача → фаза SB (Step-Back), всегда;
    - иначе — по активному learnings-доку (`learnings/active/<TASK>.md`): последняя запись фазы = текущая позиция; спроси пользователя только если по доку не ясно.
- [ ] 3. Прочитай ТОЛЬКО файл текущей фазы (`workflow/phase-*.md`) и исполняй его чекбоксы как гейты.
- [ ] 4. По завершении фазы: авто-append короткой записи в learnings (без вопроса) → объяви следующую фазу одной строкой → загрузи её файл.
- [ ] 5. `next`/`wh` от пользователя = «фаза закрыта, веди дальше»: проверь незакрытые чекбоксы текущей (фейлы/скипы — озвучь), потом переходи.
- [ ] 6. Пропуск фаз легален по правилам router'а (S-размер → сразу Phase 3), но фиксируется `[➖]` с причиной.

## Карта (для ориентировки, детали — в файлах фаз)

SB Step-Back → 0 Подход → 1 Investigation → 1.5 Grill → 2 Planning → 3 Implementation → 4 Review (3 прохода) → 5 Verify (реально прогнать) → 6 Commit → 7 Docs+Self-learn (финал: learnings, changelog, правило из ошибок).

Связанные скилы: `grill` (фаза 1.5), `confidence-calibration` (фаза 1), `mb-search`/`mb-memory` (сквозные), `memory-curator` (фаза 7, периодически).
