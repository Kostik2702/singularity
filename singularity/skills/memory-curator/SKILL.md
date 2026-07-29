---
name: memory-curator
description: Vault health check, contradiction adjudication and research agenda for the Singularity memory (Obsidian). Use when the user says curator, куратор, "проверь память", "check vault health", mdoc, "предложи связи", "что исследовать дальше", "есть ли противоречия", or periodically after large writing sessions. Runs mb curator / contradictions / gaps, reviews findings, proposes fixes - NEVER applies structure changes without user approval.
---

# memory-curator — здоровье vault, противоречия и повестка

Порт `memory-doctor` из Genevra. Три инструмента, все report-only:

| Команда | Что даёт |
|---|---|
| `mb curator [--json]` | механическая валидация + семантические предложения связей |
| `mb curator contradictions [--json] [--limit N] [--same-file]` | КАНДИДАТЫ в противоречия с evidence-токенами |
| `mb curator gaps [--json] [--top N] [--heuristic]` | чего в памяти не хватает |

## Протокол сессии курирования

- [ ] 1. Запусти `mb curator --json` и разбери вывод (issues / orphans / stale / link_suggestions).
- [ ] 2. **Битые wikilinks** — почини сам (это механика, не структура): найди правильную цель через `mb search`, исправь ссылку. Если цель не существует — спроси пользователя (удалить ссылку или создать заметку?).
- [ ] 3. **Missing frontmatter** — дополни из контекста заметки (title из H1, created из file ctime, type из папки). Файлы пользователя вне контракта (корень vault, daily) — НЕ трогай, только сообщи.
- [ ] 4. **Decisions без owner/source** — НЕ выдумывай владельца (Never Decide Alone): спроси пользователя или пометь `owner: UNRESOLVED`.
- [ ] 5. **Link suggestions** — покажи пользователю списком «A ↔ B (score)» с 1-строчным обоснованием каждой пары; применяй `[[wikilink]]` ТОЛЬКО одобренные (вставляй в секцию "Связано"/related, не в случайное место текста).
- [ ] 6. **Orphans** — предложи для каждой: связать (см. suggestions), включить в MOC, или это нормальный standalone (daily/changelog).
- [ ] 7. **Stale active** (>60 дней) — предложи `status: superseded`/archive; не менять без approve.
- [ ] 8. Итог: компактный отчёт (исправлено сам / предложено / отклонено) + запись в changelog vault, если были изменения.

## Adjudication противоречий (W07)

`mb curator contradictions` даёт пары «одна тема — разные утверждения» в полосе cosine 0.72–0.90 (выше — это near-duplicate, им занимается `mb curator`, ниже — разные темы). **Это кандидаты, а не вердикты**: precision без LLM низкая по построению, и именно поэтому каждый кандидат обязан нести `evidence` — токены, которые его вызвали.

- [ ] 1. `mb curator contradictions --json`. Читай `evidence`, а не только score: `boolean_flip`/`polarity_flip`/`negation_flip` — сильные сигналы, `numeric_mismatch` — средний, `negation_asymmetry` — модификатор.
- [ ] 2. По каждому кандидату открой ОБЕ секции целиком. Типовые ложные срабатывания: разный scope («для A включено, для B выключено» — не противоречие), сравнительная таблица, версионированный рецепт (v3 рядом с v4), цитата чужого решения.
- [ ] 3. Вердикт — только с обоснованием одной строкой. Записывать так:
```bash
mb curator verdict "<key>" --verdict false_positive --note "разный scope: A про личный vault, B про общий"
mb curator verdict "<key>" --verdict resolved      --note "поправил reference/x.md, теперь 0.45 в обоих"
mb curator verdict "<key>" --verdict accepted      --note "реальное противоречие, владелец решает"
```
Помеченная пара больше не всплывает — без этого один и тот же false positive возвращается каждый прогон и отчёт перестают читать.
- [ ] 4. **Ни одной правки заметки без approve.** Если вердикт `resolved` требует изменения — сначала покажи пользователю обе стороны и предложи, какую править.
- [ ] 5. `--same-file` включай только при аудите одной длинной заметки: внутри документа расхождение чисел почти всегда сравнительная таблица.

Опционально: батч-adjudication локальной моделью (`mb llm` + скил `mb-llm-delegate`) за флагом `lint_llm_adjudication` (по умолчанию `false`). Вердикт модели — всё равно предложение, а не применение.

## Повестка исследования (W08)

`mb curator gaps` отвечает на «что исследовать дальше», а не «прибрано ли». Шесть сигналов: `unprocessed_sources`, `open_questions`, `missing_pages`, `undigested_learnings`, `decisions_without_review`, `unlinked_from_index`.

- [ ] 1. `mb curator gaps --json`. `counts` — истинные тотали; `items` — шорт-лист после потолка на сигнал; `truncated` говорит, что скрыто.
- [ ] 2. `unprocessed_sources` → передай в скил `mb-ingest` (цикл не замкнут).
- [ ] 3. `open_questions` → предложи пользователю выбрать 1–2 и закрыть их сейчас; остальные оставь.
- [ ] 4. `missing_pages` → это страницы, на которые ссылаются ≥2 заметки и которых нет. Создавать только по approve.
- [ ] 5. `undigested_learnings` → это вход для `mb digest plan`, а не для ручной правки.
- [ ] 6. `--heuristic` — догадки по частотности, включай осознанно и помечай их как догадки в отчёте.

## Write-discipline (из Genevra memory-doctor)

- [ ] Ноль структурных записей без явного approve. Автономно можно: чинить битые ссылки с однозначной целью, дополнять механический frontmatter.
- [ ] Вердикты в `.mb/lint.json` — это состояние движка, не структура vault: их можно писать без approve, но каждый с обоснованием.
- [ ] Дашборды для ручного обзора: `_moc/Dashboard.md`, `_moc/Orphans.md` (Dataview).
