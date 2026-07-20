---
name: contrast-and-a11y-check
description: Точная проверка доступности сайта — WCAG-контраст точным расчётом (imgqa contrast), axe-прогон на живом URL (scripts/a11y-audit.mjs), протокол keyboard-навигации с задокументированными WebKit-обходами. Use when "проверь контраст", "хватает ли контраста", "a11y", accessibility check, WCAG, axe, keyboard navigation, перед релизом/визуальным baseline сайта.
---

# contrast-and-a11y-check — точная доступность, не «на глаз»

Два детерминированных инструмента + один ручной протокол. Суждение «чинить или это решение владельца» — за человеком/скилом, не за инструментами.

> **Контраст НИКОГДА не оценивается на глаз.** Прецедент NIKA: «на глаз» дал ложный fail 4.2 при реальных 4.93:1 (GT-C1 imgqa: `C9586B` на `070506` → 4.93). Только `imgqa contrast`.

## 1. Контраст (evaluative, exit 0/1/2)

```bash
imgqa contrast "#RRGGBB" "#RRGGBB" --json          # --min 4.5 по умолчанию
# verdict.result PASS/FAIL, data.ratio_raw — гейт по raw (не по округлению)
```
- AA normal ≥4.5, AA large ≥3.0, AAA ≥7.0 — читать typed-поля `aa_normal/aa_large/aaa_normal`.
- Цвета брать из computed styles/токенов проекта, не из макета «примерно».

## 2. Axe-прогон живого URL (evaluative)

```bash
# Сервер: ТОЛЬКО npx http-server (python http.server не отдаёт Range — правило vault)
node "D:\Singularity\singularity\skills\contrast-and-a11y-check\scripts\a11y-audit.mjs" \
  --project "<корень проекта>" --url http://localhost:8743 \
  --out "<evidence-dir ВНЕ проекта>" [--page /index.html] [--fail-impact serious]
```
- Playwright и axe-core берутся из node_modules ПРОЕКТА (no network/CDN); нет axe-core → typed exit 2 с ремедиацией `npm i -D axe-core`.
- exit 0 = нет нарушений ≥ fail-impact; 1 = есть (список в verdict.reason + `a11y-report.json`); 2 = инструмент не отработал (≠ «всё хорошо»).
- `?visual-test=1` добавляется автоматически (детерминизм: canvas t=0, анимации off); `--visual-test 0` — выключить.
- `data.incomplete` — axe не уверен: проверить руками, не игнорировать молча.

## 3. Keyboard-nav протокол (ручной, Playwright-only)

Интерактивный Chrome — не источник истины (правило vault «аномалия в интерактивном Chrome — лид»). Проверять Playwright-скриптом в контексте проекта: Tab-обход всех интерактивных элементов, видимый focus ring, Escape закрывает модалки, Enter/Space активируют.

**WebKit-ловушки (vault [[WEBKIT-TRAPS]], НЕ переисследовать):**
- `button` в WebKit не получает focus по клику — проверять Tab-порядком, не кликом;
- синтетические Tab/Space в headless WebKit ненадёжны — гонять keyboard-тесты в Chromium, WebKit только смоук;
- `page.route` не перехватывает media-запросы (.mp4) в WebKit — «media 404» тест там молча не работает.

## Гейты

- [ ] Все цветовые пары текста — через `imgqa contrast`, числа в отчёт.
- [ ] axe exit 2 ≠ PASS; различать «чисто» и «не отработало».
- [ ] Нарушение может быть B-категорией (намеренное решение владельца) — сверить с vault до «чинить»; финальный вердикт — human gate.
- [ ] Результаты — в learnings (`mb learn`), a11y-report.json как evidence.
