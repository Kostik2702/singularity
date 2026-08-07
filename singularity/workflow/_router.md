---
type: reference
title: "Workflow Router — соло-разработка"
tags: [workflow, router, singularity]
created: 2026-07-03
status: active
source: "порт Genevra COMPLETE_IMPLEMENTATION_WORKFLOW (12 фаз → 8 под соло)"
---

# Workflow Router — соло-разработка

**Правило C9 (lazy-load):** этот файл только маршрутизирует. Загружай файл фазы ТОЛЬКО при входе в неё — не читай все фазы разом. Помощник: skill `workflow-helper` (fw / wh / next).

## Карта фаз

| # | Фаза | Файл | Триггер входа |
|---|---|---|---|
| SB | Step-Back | `phase-SB-step-back.md` | любая новая задача — ВСЕГДА первым |
| 0 | Выбор подхода | `phase-0-approach.md` | после Step-Back |
| 1 | Investigation | `phase-1-investigation.md` | подход = research/bug |
| 1.5 | Grill | `phase-1-5-grill.md` | перед планом/дизайном (skill `grill`) |
| 2 | Planning | `phase-2-planning.md` | investigation закрыта |
| 3 | Implementation | `phase-3-implementation.md` | план одобрен |
| 4 | Review | `phase-4-review.md` | код написан |
| 5 | Verify | `phase-5-verify.md` | ревью пройдено |
| 6 | Commit | `phase-6-commit.md` | verify зелёный |
| 7 | Docs + Self-learn | `phase-7-docs-selflearn.md` | закоммичено / задача закрыта |

## Сквозные правила (действуют во всех фазах)

- [ ] Активный learnings-док (`learnings/active/<TASK>.md`) существует с фазы SB; каждая фаза ЗАКАНЧИВАЕТСЯ авто-append короткой записи в него (без вопроса пользователю).
- [ ] [[Core Discipline - SEM-FIRST]] · [[Core Discipline - Never Decide Alone]] · [[Core Discipline - Checkbox Gates]] · [[Core Discipline - Token Economy]].
- [ ] Фаза не «пройдена», пока её чекбоксы не `[x]`+evidence / `[➖]`+причина.
- [ ] **Топливный отчёт:** закрывая фазу, печатай одну строку — `⛽ <фаза>: N токенов`. Источник — `mb usage --session <id> --brief --from <индекс первого хода фазы>`: команда сама печатает одну строку и ничего больше. `--session` ОБЯЗАТЕЛЕН: по умолчанию берётся самая свежая сессия на машине, и параллельно запущенная чужая работа подменит цифры (проверка: id в начале строки должен совпасть с текущей сессией). Индекс берётся из `--brief` предыдущей фазы, так что арифметики и промежуточных файлов не нужно. `--json` в контекст не выводить никогда: он весит ~8k токенов, то есть измерение расхода становится главной статьёй расхода.
- [ ] Пропуск фазы — легален (мелкая правка не требует plc), но помечается `[➖]` с причиной в learnings.

## Отличия от Genevra (12 фаз, команда)

Убрано: PR-description для команды, related-repos, persistent-QA, demo-prep, memory-PR, командное ревью. Добавлено: явный verify-гейт (реально прогнать), self-learn в финале. Решение: [[00_Vision_And_Principles]] (owner: user).
