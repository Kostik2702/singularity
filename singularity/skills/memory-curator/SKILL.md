---
name: memory-curator
description: Vault health check and knowledge-graph curation for the Singularity memory (Obsidian). Use when the user says curator, куратор, "проверь память", "check vault health", mdoc, "предложи связи", or periodically after large writing sessions. Runs mb curator, reviews findings, proposes fixes - NEVER applies structure changes without user approval.
---

# memory-curator — здоровье vault и курирование графа

Порт `memory-doctor` из Genevra. Инструмент: `mb curator [--json]` — механическая валидация + семантические предложения (по существующему индексу, мгновенно).

## Протокол сессии курирования

- [ ] 1. Запусти `mb curator --json` и разбери вывод (issues / orphans / stale / link_suggestions).
- [ ] 2. **Битые wikilinks** — почини сам (это механика, не структура): найди правильную цель через `mb search`, исправь ссылку. Если цель не существует — спроси пользователя (удалить ссылку или создать заметку?).
- [ ] 3. **Missing frontmatter** — дополни из контекста заметки (title из H1, created из file ctime, type из папки). Файлы пользователя вне контракта (корень vault, daily) — НЕ трогай, только сообщи.
- [ ] 4. **Decisions без owner/source** — НЕ выдумывай владельца (Never Decide Alone): спроси пользователя или пометь `owner: UNRESOLVED`.
- [ ] 5. **Link suggestions** — покажи пользователю списком «A ↔ B (score)» с 1-строчным обоснованием каждой пары; применяй `[[wikilink]]` ТОЛЬКО одобренные (вставляй в секцию "Связано"/related, не в случайное место текста).
- [ ] 6. **Orphans** — предложи для каждой: связать (см. suggestions), включить в MOC, или это нормальный standalone (daily/changelog).
- [ ] 7. **Stale active** (>60 дней) — предложи `status: superseded`/archive; не менять без approve.
- [ ] 8. Итог: компактный отчёт (исправлено сам / предложено / отклонено) + запись в changelog vault, если были изменения.

## Write-discipline (из Genevra memory-doctor)

- [ ] Ноль структурных записей без явного approve. Автономно можно: чинить битые ссылки с однозначной целью, дополнять механический frontmatter.
- [ ] Дашборды для ручного обзора: `_moc/Dashboard.md`, `_moc/Orphans.md` (Dataview).
