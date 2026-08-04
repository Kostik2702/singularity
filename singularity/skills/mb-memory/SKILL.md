---
name: mb-memory
description: Capture knowledge into the user's Obsidian memory vault (Singularity). Use when the user says um, запомни в vault, "запиши в память", "add to memory brain", after completing a phase of work on the Singularity project, or when an important decision/insight/mistake emerges that should persist. Defines what deserves recording and the note contract.
---

# mb-memory — запись знаний в vault

Vault резолвится сам (`--vault` → `MB_HOME` → `~/.singularity.json` → дефолты); где он на этой машине — скажет `mb doctor` (чек `A2-vault-obsidian`). Путь здесь не хардкодится: до 2026-08-04 в этой строке стоял абсолютный путь одной машины, который на macOS/Linux указывал в никуда.

Watcher индексирует сохранённое автоматически (~5-15 с) — после записи ничего запускать не нужно.

## Что записывать (токен-критерий из Genevra)

- [ ] Записывай ТОЛЬКО: неочевидное / противоречащее ожиданиям / потребовавшее >1 попытки / решение с последствиями.
- [ ] НЕ записывай: то, что видно из git/кода, тривиальные факты, пересказ файлов. Код — только как `file:line`.
- [ ] Перед записью — `mb search "<суть>"`: если найдено с score >0.6 — дополни существующую заметку, не создавай дубль.

## Как записывать

```bash
mb learn "<плотный инсайт одной-тремя фразами>" --task <TASK-ID>   # быстрый append в learnings/active/<TASK-ID>.md
mb note new decision "Название решения"    # новая заметка из шаблона
# типы: learning|decision|howto|incident|rule|reference|daily|moc|changelog — по одному на каждый managed-корень
```

- [ ] Личное (не относится ни к одному рабочему проекту) — добавь `--project personal` к `mb learn`/`mb note new`, или, если запись уже создаётся в каталоге из `personal_roots` (см. `.mb/config.json`), routing применится автоматически. Личные заметки никогда не попадают в digest/curator-предложения и никогда не резолвятся случайным cwd-детектом — только explicit `--project personal` или personal_roots.

### GATE перед любой записью: чем писать

- [ ] **Новый файл в managed-корне** (`learnings/*`, `decisions/`, `howto/`, `incidents/`, `rules/`, `reference/`, `changelog/`, `_moc/`, `daily/`) — ТОЛЬКО `mb note new <type> "<title>"`. Размер записи роли не играет: создай файл командой, а наполняй уже Edit'ом.
- [ ] **Правка существующей заметки** — Edit/Write, это штатный путь.
- [ ] **Конфиг** — `mb config set` / `mb project add`, не правка `.mb/config.json`.
- [ ] **Искать в vault** — `mb search`, `mb curator gaps` (битые ссылки, недайджещенные learnings), `mb curator contradictions`, а не `grep`/обход файлов.

Почему это гейт, а не рекомендация: мимо `mb note new` не применяется `templates/<type>.md` — и запись НЕ падает, просто рождается без `source:` и без обязательных секций (для rule — `## Правило` / `## Почему` / `## Происхождение` с GATE). Такую заметку не отличить от полной, пока кто-нибудь не сверит её с шаблоном руками; ровно так и произошло 2026-08-04. Плюс теряются project-routing и dedup.

`vault_guard` (audit, включён по умолчанию с v0.6.3) отвечает на такой Write строкой `[vault-guard audit] New file created directly in managed knowledge root …` — это сигнал остановиться и пересоздать командой, а не фоновый шум.

Наполняя заметку, соблюдай контракт:

- [ ] Frontmatter обязателен: `type, title, tags, created`; для decision — `owner` и `source` (Never Decide Alone: у решения есть автор и происхождение).
- [ ] Связи — inline `[[wikilinks]]` на существующие заметки (питают граф Obsidian и graph-фильтр поиска).
- [ ] Изменение значимого — строка в `changelog/YYYY-MM.md` (единственная история; сами заметки не раздуваем историей правок).
- [ ] Устаревшее не удалять: `status: superseded` + ссылка на замену.

## Ошибки → правила (самообучение)

- [ ] Если ошибка ассистента повторилась или стоила времени: создай `mb note new rule "..."` и ВЖИВИ правило в конкретную точку исполнения (этот skill / CLAUDE.md / соответствующая заметка), а не в общий список. GATE: «поймало бы это правило именно этот сбой?» — если нет, правило не нужно.

## Конец сессии по проекту

- [ ] Один вопрос пользователю: «Финализировать learnings?» — и по ответу дописать итог сессии в активный learnings-док.
