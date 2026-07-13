---
name: mb-memory
description: Capture knowledge into the user's Obsidian memory vault (Singularity). Use when the user says um, запомни в vault, "запиши в память", "add to memory brain", after completing a phase of work on the Singularity project, or when an important decision/insight/mistake emerges that should persist. Defines what deserves recording and the note contract.
---

# mb-memory — запись знаний в vault

Vault: `D:\obsidian\memory brain`. Watcher индексирует сохранённое автоматически (~5-15 с) — после записи ничего запускать не нужно.

## Что записывать (токен-критерий из Genevra)

- [ ] Записывай ТОЛЬКО: неочевидное / противоречащее ожиданиям / потребовавшее >1 попытки / решение с последствиями.
- [ ] НЕ записывай: то, что видно из git/кода, тривиальные факты, пересказ файлов. Код — только как `file:line`.
- [ ] Перед записью — `mb search "<суть>"`: если найдено с score >0.6 — дополни существующую заметку, не создавай дубль.

## Как записывать

```bash
mb learn "<плотный инсайт одной-тремя фразами>" --task <TASK-ID>   # быстрый append в learnings/active/<TASK-ID>.md
mb note new decision "Название решения"    # новая заметка из шаблона (learning|decision|howto|incident|rule|reference|daily)
```

- [ ] Личное (не относится ни к одному рабочему проекту) — добавь `--project personal` к `mb learn`/`mb note new`, или, если запись уже создаётся в каталоге из `personal_roots` (см. `.mb/config.json`), routing применится автоматически. Личные заметки никогда не попадают в digest/curator-предложения и никогда не резолвятся случайным cwd-детектом — только explicit `--project personal` или personal_roots.

Крупные записи — редактируй `.md` напрямую (Edit/Write), соблюдая контракт:

- [ ] Frontmatter обязателен: `type, title, tags, created`; для decision — `owner` и `source` (Never Decide Alone: у решения есть автор и происхождение).
- [ ] Связи — inline `[[wikilinks]]` на существующие заметки (питают граф Obsidian и graph-фильтр поиска).
- [ ] Изменение значимого — строка в `changelog/YYYY-MM.md` (единственная история; сами заметки не раздуваем историей правок).
- [ ] Устаревшее не удалять: `status: superseded` + ссылка на замену.

## Ошибки → правила (самообучение)

- [ ] Если ошибка ассистента повторилась или стоила времени: создай `mb note new rule "..."` и ВЖИВИ правило в конкретную точку исполнения (этот skill / CLAUDE.md / соответствующая заметка), а не в общий список. GATE: «поймало бы это правило именно этот сбой?» — если нет, правило не нужно.

## Конец сессии по проекту

- [ ] Один вопрос пользователю: «Финализировать learnings?» — и по ответу дописать итог сессии в активный learnings-док.
