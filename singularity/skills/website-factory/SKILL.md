---
name: website-factory
description: Оркестратор конвейера производства motion/3D-сайтов — ведёт проект по state-graph E09 от брифа до release-evidence через типизированные стадии (scout → анализ → профиль → shortlist → карантин → рецепт → реализация → QA-гейты → baseline → golden), с resume-points и жёсткими human gates. Use when "сделай сайт по конвейеру", website factory, старт нового сайта/секции фабрикой, "какая следующая стадия", возобновление конвейера после решения владельца.
---

# website-factory — конвейер, не вдохновение

Канон маршрута: vault [[WEBSITE-FACTORY-E09-STATE-GRAPH]] (S1-S10). Оркестратор НЕ выдумывает шаги — он ведёт по графу, где каждый stage = существующий инструмент с typed-выходом, и **останавливается на каждом human gate**. Resume-point любой стадии — её выходной файл; упавший провайдер/стадия не обнуляет прогон.

> Перед стартом: `mb search "<проект>"` (SEM-FIRST) — прежние решения/бюджеты/бриф. Все прогоны пишутся в learnings проекта.

## Маршрут (инструмент → выход → гейт)

| S | Действие | Инструмент | Выход (resume) |
|---|---|---|---|
| 0 | Brief contract от владельца | шаблон §1 решения v2 | бриф-заметка |
| 1 | Визуальные референсы | `design-scout collect` | moodboard.html → **⛔ owner** |
| 2 | Разбор макета (если есть) | скил design-analysis | design-brief → **⛔ owner** |
| 3 | Профиль стека | `stackrouter` по `.factory/manifest.json` | recommendation; смена профиля → **⛔ owner** |
| 3b | Scaffold | `starters/<profile>` (copy) + скил visual-regression-bootstrap | рабочий каркас + harness |
| 4 | Кандидаты паттернов | `pattern-scout search --board --profile` | review-board.html → **⛔ owner shortlist** |
| 5 | Карантин каждого выбранного | `component-intake run --runtime` | intake-report; REJECT = конец ветки |
| 6 | Рецепт | `component-intake recipe --report` → скил pattern-adapter → `--validate` (exit 0) | recipe.json |
| 7 | Реализация | скил motion-3d-implementer (feature branch проекта) | код по patch_set |
| 8 | Гейты качества | motion-qa (+бюджеты проекта) · determinism-чекер · a11y-audit · fidelity-audit | отчёты; FAIL → итерация S7 (**cap 3**, потом эскалация) |
| 9 | Visual baseline + приёмка слайса | — | **⛔ owner-only** (`test:update-baseline` запускает владелец) |
| 10 | Реестр | `golden add` (candidate) → 2-е применение/owner → `golden promote` | registry item |

Git-дисциплина: каждая задача — feature branch → merge в epic/проект по готовности; движковые изменения — только через epic/website-factory.

## Правила оркестратора

- **Гейты не обходятся.** Нет owner-ответа → стадия ждёт; работать можно над другой веткой графа (per-candidate независимость). Draft-default допустим ТОЛЬКО с записью в decisions/ с маркировкой ⚠️draft (прецедент [[WEBSITE-FACTORY-DRAFT-GATES-2026-07-19]]).
- **Typed или не было.** Выход стадии без конверта/файла = стадия не пройдена. exit 2 любого инструмента = починить инструмент/окружение, не «пропустить».
- **0 one-off скриптов** (KPI WSF-13): всё уже есть субкомандами; новый скрипт = регрессия конвейера.
- **Возможность живёт в движке, скил маршрутизирует.** Не хватает функции — это изменение инструмента (`pattern-scout`, `component-intake`, `imgqa`, harness), а не ещё один скрипт в папке скила. Прецедент цены: 8 скриптов стека `bridge-*`, из них 4 дублировали более зрелые подкоманды, а 2 держали вторую копию геометрии кропа.
- **Лицензии:** unknown = reference-only на ЛЮБОЙ стадии; paid — report-only до owner. Лицензия читается ДО рекомендации кандидата, а не перед вендорингом; `NOASSERTION`/`Other` на GitHub = «не распознано», повод открыть `LICENSE`, а не считать проект пермиссивным.
- **Манифест ассетов генерируется обходом дерева, не руками:** генератор падает на любом неучтённом/необъяснённом файле (coverage self-check). Ручной манифест дрейфует молча, и «100% coverage» становится ложью на первом же добавленном файле; правка манифеста руками = code smell на ревью.
- **Метрики прогона** (для WSF-13/E10): время до shortlist/первой секции/билда, PASS/ADAPT/REJECT, итерации S7-S8, число owner-вопросов — в learnings каждого прогона.

## Прецедент полного прохода (GT)

2026-07-19, MATRIX + NIKA: S4 board (57) → S5 intake LetterGlitch (ADAPT, runtime чист) → S6 рецепт (exit 0) → S7 vanilla island → S8 motion-qa PASS 9/9 + determinism STABLE → S10 golden через второе применение (NIKA retrofit, no-regression байт-точно). Два дефекта поймано гейтами по пути. Это эталон исполнения конвейера.
