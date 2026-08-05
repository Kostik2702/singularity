---
name: pattern-source-scout
description: Бриф → воспроизводимый shortlist инсталлируемых motion/3D/component-паттернов из внешних экосистем (21st, React Bits, three.js examples, Motion Primitives, GSAP) с лицензиями, score 100 и HTML review board для приёмки владельцем. Тонкая обёртка над CLI `pattern-scout`. Use when "найди компоненты", "какие есть готовые паттерны", pattern shortlist, "installable примеры под бриф", подбор motion/3D кандидатов до adaptaion/intake.
---

# pattern-source-scout — бриф → shortlist кандидатов

Вся детерминированная работа — CLI `pattern-scout` (движок, контракт `singularity/providers/CONTRACT.md`, provider-catalog/1). Скил: строит запросы, интерпретирует score/hard-fails, ведёт review board к owner gate. **Найденный код НИКОГДА не исполняется на этом этапе** — установка только через quarantine intake (E03).

> **Правило нулевого кода:** поиск/нормализация/кэш/score/board — субкоманды `pattern-scout`. Новый источник = строка registry.json. 0 новых скриптов.

## Порядок

```bash
pattern-scout probe --json     # здоровье провайдеров (evaluative: >=1 live)

pattern-scout search \
  --query "code rain" --query "terminal background" --query "scroll narrative" \
  --capability particles --capability scroll-linked \
  --out "<evidence-dir ВНЕ проекта>" --board --profile static-lite
# PSCOUT_PROTECTED_ROOTS=<project> запрещает запись в проект.
# -> candidates.json (typed) + review-board.html (scored, для владельца)
```

## Query expansion (LLM-часть)

Из брифа/мудборда — 3-6 коротких EN-запросов: приём («code rain»), поверхность («terminal background»), паттерн («scroll narrative»). RU дублировать не надо (экосистемы EN). Capability-фильтр из таксономии §3 контракта — опечатка в capability даёт громкий exit 2, не пустой результат.

## Как читать выдачу

- `importable=false` + `import_block_reason` — жёсткий гейт §2: `license unknown` (весь 21st!), paid (report-only), reference-only, remote-code. Это НЕ дефект кандидата — это статус «нельзя тащить без отдельной проверки».
- `stale=true` — кандидат из кэша при недоступном провайдере; перед shortlist перепроверить живьём.
- Score 100 — только для сортировки shortlist (компоненты и evidence в details карточки): brief-fit 25 / license 20 / stack 15 / perf 15 / deps 10 / a11y 10 / freshness 5. Неизвестное получает нейтральный default, никогда max — кандидат не выигрывает на отсутствии данных.
- `providers` statuses: `unavailable:*+cache` — провайдер лежит, выдача из кэша; run валиден.

## Гейты

- [ ] Никакой установки/исполнения кандидатов — только метаданные и board. Установка = E03 quarantine.
- [ ] `license=unknown` не помечается importable ни при каких условиях (контракт enforce'ит, скил не обходит).
- [ ] **Лицензия читается ДО того, как кандидат назван в рекомендации, а не перед вендорингом.** К моменту проверки решение уже принято, и отзывать рекомендацию дороже, чем не давать её. Поле лицензии на GitHub — результат автоопределения: `Other`/`NOASSERTION` значит «GitHub её не узнал», а не «лицензии нет», и требует открыть сам `LICENSE` и прочитать текст на коммерческое использование, раскрытие, требование спонсорства, вирусность (GPL/AGPL).
- [ ] Shortlist/art-direction подтверждает владелец по review-board.html (human gate §12) — board сам по себе не approval.
- [ ] Прогон (запросы, статусы, счётчики, путь board) — в learnings через `mb learn`.
