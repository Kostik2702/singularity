---
name: mb-onboard
description: Register a new project in the Singularity vault and give it a persistent SessionStart brain note. Use when the user says onboard this project, "заведи проект в память", "подключи этот репозиторий к памяти", or starts working in a codebase that mb project detect doesn't yet recognize.
---

# mb-onboard — регистрация нового проекта

Через sanctioned CLI (`mb project`, `mb note new`) плюс ручное редактирование `.mb/context.json` (для него пока нет отдельной CLI-команды — редактируй Edit/Write). Никогда не используй fork-only пути вида `code/...`; проект — это то, что зарегистрировано в registry, а не то, что лежит в каком-то конкретном каталоге.

## Протокол

- [ ] 1. Определи `<id>` (короткий, стабильный, без пробелов), путь к проекту, опционально `--group`/`--about`.
- [ ] 2. Зарегистрируй: `mb project add <id> <path> [--group G] [--about "..."]`. Если id уже занят или путь пересекается с другим проектом — `mb project list` покажет конфликт, не форсируй перезапись.
- [ ] 3. Создай starter brain-заметку: `mb note new reference "<Project> Brain"` — коротко (цель проекта, ключевые пути/команды, архитектурные решения, которые агент должен знать в КАЖДОЙ сессии этого проекта). Это не документация проекта целиком — только то, что стоит бюджета `brain_max_lines` (по умолчанию 40 строк, `mb config get brain_max_lines`).
- [ ] 4. Пропиши bridge в `.mb/context.json` (Edit/Write, atomic — читай файл, дополняй объект, не перезаписывай другие проекты): `{"<id>": {"brain_note": "<vault-relative путь к заметке из шага 3>"}}`. Без этого шага SessionStart не найдёт заметку — `mb project add` регистрирует ТОЛЬКО путь проекта, не brain note.
- [ ] 5. Проверь: `mb project detect <path проекта>` должен вернуть `<id>`; следующая сессия в этом каталоге должна показать "Project brain (`<id>`)" в SessionStart-контексте.
- [ ] 6. Сообщи пользователю одной строкой: проект зарегистрирован, brain note создан, что в нём стоит дополнить со временем.

## Не делай

- [ ] Не создавай project id `general`/`personal` — они зарезервированы (RESERVED_IDS), `mb project add` их отклонит.
- [ ] Не пиши в `.mb/context.json` без предварительного чтения текущего содержимого — это shared-файл, перезапись стирает другие проекты.
- [ ] Module-context (`context_map` внутри записи проекта, per-module notes) — отдельная, более редкая настройка; не добавляй её проактивно, только если пользователь явно просит per-module context.
