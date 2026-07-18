---
name: design-reference-scout
description: Сбор дизайн-референсов из открытых источников (Openverse, Are.na, Httpster, Minimal.gallery, Land-book, опционально Unsplash/Pexels) в HTML-мудборд с провенансом и лицензиями для приёмки владельцем. Тонкая обёртка над детерминированным CLI `design-scout`. Use when "собери референсы", "мудборд для дизайна", "design references", "вдохновение для сайта", art-direction поиск примеров, старт визуального дизайна нового сайта/секции.
---

# design-reference-scout — мудборд из открытых источников

Вся детерминированная работа — в CLI `design-scout` (движок Singularity, контракт `singularity/scout/CONTRACT.md`, **schema_version 1**). Скил не содержит логики: он говорит КОГДА применять, КАК строить запросы, КАК читать вывод и ГДЕ human-gate.

> **Правило нулевого кода (trajectory-инвариант WSF-13):** тянет написать скрипт «сходить на сайт/скачать превью/склеить HTML» — СТОП, это уже субкоманды `design-scout`. Новый источник = строка в `sources.json`, не код. За скаут-сессию — **0 новых скриптов**.

## Когда применять

- Старт дизайна нового сайта/секции: нужен мудборд под art-direction.
- Пополнение референсов по конкретной эстетике («dark terminal», «brutalist», «minimal»).
- Не для: сбора инсталлируемых компонентов/кода (это WSF-E02 pattern-source-scout), не для ассетов прод-качества (это лицензионная закупка/съёмка).

## Порядок

```bash
# 1. Здоровье источников (evaluative: floor «≥3 живых, ≥1 API» из реестра)
design-scout probe --json

# 2. Сбор: 3-6 коротких запросов, EN обязательно, RU по желанию
design-scout collect \
  --query "matrix terminal aesthetic" --query "code rain" \
  --query "dark minimal web design" \
  --out "D:\Project\<project>-evidence\scout-<date>"
# --out ВНЕ дерева проекта; DSCOUT_PROTECTED_ROOTS=<project> запрещает запись в проект физически

# 3. Открыть moodboard.html, отсмотреть, вынести на приёмку владельцу
```

## Как строить запросы (LLM-часть — единственная законная)

- Разворачивай art-direction бриф в **короткие (1-3 слова) конкретные** запросы: эстетика («dark terminal»), приём («code rain»), жанр («brutalist portfolio»).
- EN-запросы обязательны (источники англоязычны); RU — дополнение для Openverse.
- Длинные фразы («красивый тёмный сайт с анимацией») дают шум/пустоту — дели на атомы.
- Httpster не ищет — он роутится по ключевым словам стиля (dark, minimal, brutalist, typographic, retro, interactive…): включай хотя бы одно такое слово в набор запросов.

## Как читать вывод (контракт §2, §6)

- Пинить `schema_version == 1`; незнакомая версия = громкий стоп.
- `collect` exit 0 при **завершённом прогоне**, даже если часть источников `unavailable:*` — смотри `data.sources`: typed-статусы (`live`, `unavailable:http-403`, `skipped:no-key`, `skipped:disabled`, `skipped:budget`).
- exit 2 = инструмент не отработал (реестр битый, --out запрещён). Это НЕ «нет результатов».
- `data.budget_log` — доказательство соблюдения бюджетов (fetches/retries/thumbs/elapsed/stop_condition) — фиксируй в learnings прогона.

## Лицензии (OD-2, owner 2026-07-14 — не переинтерпретировать)

| Бейдж policy в мудборде | Что можно |
|---|---|
| `api-free-license` (Openverse, Pexels) | превью скачано локально, лицензия у каждой записи — использовать по её условиям, attribution обязателен |
| `api-hotlink` (Unsplash) | только hotlink-embed их URL + attribution; скачивание запрещено ToS |
| `links-only` (галереи) | ссылка/embed для вдохновения; **скачивание и копирование дизайна запрещено** — браузинг ≠ право хранения |
| `per-block` (Are.na) | по лицензии блока; unknown = только ссылка |

`license: unknown` НИКОГДА не значит «можно использовать».

## Owner gates (Never Decide Alone)

1. **Приёмка мудборда** — вход для дизайна утверждает владелец; мудборд сам по себе не approval.
2. **Godly / SiteInspire** выключены: их robots.txt запрещает AI-краулеров. Включение — только явное решение владельца (правка `enabled` в sources.json с записью в vault).
3. **Unsplash/Pexels** — включаются ключами `UNSPLASH_ACCESS_KEY` / `PEXELS_API_KEY` (владелец заводит аккаунты; demo-лимиты 50/200 req/h).
4. Платные источники — вне scope, report-only (политика v2 expansion).

## Фиксация результата

Прогон (запросы, статусы источников, budget_log, путь мудборда) — в learnings задачи через `mb learn`. Принятый владельцем мудборд линкуется в design-brief (вход WSF-06 design-analysis).
