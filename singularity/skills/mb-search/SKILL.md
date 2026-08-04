---
name: mb-search
description: Semantic search over the user's Obsidian memory vault (Singularity, CLI mb). Use when the user says sem, fm, "найди в памяти", "find in memory", "что мы знаем о", "what do we know about", or BEFORE any grep/Read discovery of prior knowledge, decisions, howtos, incidents, project docs. RU and EN queries both work.
---

# mb-search — семантический поиск по памяти

Вольт резолвится сам (`--vault` → `MB_HOME` → `~/.singularity.json`). Демон отвечает ~10-30 мс warm; при мёртвом демоне клиент сам его поднимет (первый запрос ~10 с) или ответит cold.

## SEM-FIRST правило

- [ ] Для ЛЮБОГО поиска знаний (память, решения, прошлые задачи, howto) — сначала `mb search`, и только потом grep/Glob/Read как fallback.
- [ ] Пропускать sem можно только когда путь уже известен: назван пользователем, получен из предыдущего результата, или детерминирован задачей.

## Команды

```bash
mb search "как настроен поиск"            # top-5, compact-выдача (экономия токенов)
mb search "reload bug" --top 10           # больше результатов
mb search "..." --type decision           # только решения (learning|decision|howto|incident|rule|reference)
mb search "..." --tags search,daemon      # фильтр по тегам (frontmatter + inline)
mb search "..." --links "Doctor_Plan"     # только заметки, связанные с [[Doctor_Plan]]
mb search "..." --path-prefix 03_Tasks/   # только в папке
mb search "..." --json                    # полные объекты (для обработки)
mb search "..." --full                    # без обрезки excerpt
```

Выдача compact: `file | score | [type] | title/section | excerpt(150)`. Score < ~0.3 — слабое совпадение; переформулируй запрос (модель мультиязычная, можно смешивать RU/EN).

## Обслуживание (если поиск странный)

```bash
mb daemon status          # жив ли демон, порт
mb index update           # инкрементально доиндексировать (обычно не нужно — watcher следит)
mb daemon reload          # перечитать индекс
mb doctor --fix           # полная диагностика + ремонт
```

- [ ] Результат для пользователя цитируй как `файл | секция`, не вываливай сырые строки.
