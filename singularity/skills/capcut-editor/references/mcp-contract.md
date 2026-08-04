# CapCut MCP contract

Это capability-контракт, а не привязка к имени сервера. CapCut MCP обычно
неофициальны и могут менять tool names вместе с внутренним форматом draft.
Сначала сопоставить фактически доступные tools с семантикой ниже.

## Профили

| Capability | general-draft | talking-head |
|---|---:|---:|
| создать новый draft | required | — |
| добавить video | required | — |
| сохранить в новый target | required | — |
| list/open/inspect project | optional | required |
| клонировать или адресовать отдельный target | required для existing draft | required для safe mutation |
| удалить паузы/дубли по captions | optional | required |
| readback timeline или проверяемый save result | required | required |

talking-head не является общим монтажным профилем. Не использовать его для
сборки произвольной многодорожечной timeline.

## Семантические capabilities

### Базовые

- create_draft(width, height, fps?, name?) -> draft_id|draft_path
- add_video(draft_id, source, source_in, timeline_in, duration, ...)
- save_draft(draft_id, target?) -> draft_path
- readback(draft_id|draft_path) -> canvas, fps, duration, tracks/items либо проверяемый структурированный save result

### Условные

- get_media_duration(source) — обязателен, если длительности не доказаны локально.
- add_audio, add_image, add_text, add_subtitle — обязательны, если соответствующий тип есть в плане.
- add_effect, add_sticker, add_video_keyframe — обязательны только для запрошенных приёмов.
- list_projects, open_project, clone_project — обязательны для безопасной работы с существующим draft.
- import/copy media — обязателен при media_policy=copy; иначе plan должен явно сохранять абсолютные внешние зависимости.

Названия create_draft, add_video, add_audio, add_text, add_subtitle,
add_video_keyframe, save_draft характерны для CapCutAPI-compatible серверов,
но совпадение имени не доказывает совпадение аргументов. Прочитать tool schema
до вызова.

SmartCut-compatible серверы обычно дают list/open/inspect/smart_cut и подходят
только для talking-head workflow по уже созданным auto captions.

## Evidence levels

| Level | Evidence | Verdict |
|---|---|---|
| A | timeline readback с tracks/items и временами | можно сравнить с plan автоматически |
| B | typed success каждого write + существующий новый draft path | provisional; обязателен просмотр в CapCut |
| C | только свободный текст или in-place mutation без target/readback | unsupported, STOP |

Никогда не повышать B до A по уверенности модели. Unknown/unavailable capability
не означает success.

## Ошибки

- [ ] Сохранить tool name, безопасные аргументы, typed error и последний доказанный state.
- [ ] Не повторять mutation автоматически, если tool может быть non-idempotent.
- [ ] После partial failure не продолжать следующие tracks. Сохранить target как диагностический артефакт и сообщить точку остановки.
