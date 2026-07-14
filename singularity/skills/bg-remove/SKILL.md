---
name: bg-remove
description: Удаление фона с изображения с автоматическим QA краёв — rembg/BiRefNet в изолированном ML-venv через imgqa cutout, затем probe/edges/backgrounds/detect-fringe и при необходимости локальный defringe. Use when "убери фон", "сделай PNG с прозрачностью", "вырежи объект/человека", background removal, cutout, "нужен прозрачный фон", подготовка hero/product ассета из фото.
---

# bg-remove — вырезка фона с QA-пайплайном

Вся работа — командами `imgqa` (контракт schema 2). ML-движок (rembg + BiRefNet, ~930MB весов) живёт в изолированном venv `D:\venvs\imgqa-ml` (OD-1) — движок Singularity его не импортирует, только subprocess-dispatch.

> **Правило нулевого кода:** вырезка/QA/ремонт — только субкоманды `imgqa`. Никаких самописных скриптов с rembg.

## Что модель умеет и не умеет (по spike 2026-07-14)

- **Целевой кейс — фото-субъекты:** люди, объекты. Волосы/пряди — хорошо (проверено на видео-кадре с распущенными волосами и белой одеждой на светлом фоне); ~9с/изображение на CPU.
- **Line-art/логотипы на однотонном фоне** — работает (свадебное приглашение вырезано чисто).
- **НЕ целевой кейс:** страницы с текстом/графикой на белом — BiRefNet это salient-object модель, текст «съедается» частично. Для таких входов вырезка не имеет смысла — сказать это пользователю, не гнать пайплайн.

## Пайплайн

```bash
# 0. (однократно на машине) ML-venv должен существовать: D:\venvs\imgqa-ml
#    exit 2 с ремедиацией = venv нет; создать по инструкции из ошибки. Веса на D:.

# 1. Вырезка (dispatch, ~15с холодный старт)
imgqa cutout PHOTO.png --out OUTDIR --json
#    exit 2 = инструмент не отработал (нет venv/зависимостей/битый вход) — НЕ «фон уже чистый»

# 2. Авто-QA результата (скил image-asset-qa, шаги 1-3):
imgqa probe OUTDIR/PHOTO.cutout.png --json           # настоящая alpha?
imgqa backgrounds OUTDIR/PHOTO.cutout.png --out QADIR # 3 фона
imgqa edges OUTDIR/PHOTO.cutout.png --out QADIR       # 200% кропы краёв
imgqa detect-fringe OUTDIR/PHOTO.cutout.png --json    # ореолы?

# 3. При детекциях: щедрый bbox (>=3x feather) → локальный ремонт
imgqa defringe OUTDIR/PHOTO.cutout.png --bbox X,Y,W,H --out OUTDIR
#    → повторить detect-fringe на результате (ожидаем 0)

# 4. Экспорт responsive-вариантов (WebP/AVIF) — если нужен, ffmpeg/Pillow-плагин
```

Гейт `--out`: QADIR не может быть внутри каталога вырезки (политика источника — imgqa сам откажет).

## Human gate (обязателен)

Финальную приёмку краёв — особенно **matting волос** — делает человек по QA-evidence (3 фона + edges-кропы, смотреть глазами через Read). Скил никогда не объявляет края «готовыми»: детектор ловит класс «плотный ореол», качество тонких прядей автоматом не оценивается. Показать пользователю рендер на контрастном фоне + 1-2 edge-кропа и спросить приёмку.

## Ловушки (проверены spike'ом)

- TLS-прокси машины ломает скачивание весов (pip и pooch): pip лечится `pip.ini` с trusted-host (уже в venv), веса — `curl -L --ssl-no-revoke` в `D:\venvs\imgqa-ml\models`, **имя файла = `<model>.onnx`** (pooch ищет по fname, не по имени релиза; md5 проверяется).
- exit 2 ≠ pass. Пустой/невалидный вывод ML-venv = tool error (валидируется движком: schema/status/command/outputs/файлы внутри --out).
- Вердикт QA → `mb learn --task <TASK>`; дефекты на dogfood — в regression-каталог (§9).
