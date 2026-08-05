---
name: visual-regression-bootstrap
description: Развернуть в static-site проект готовый Playwright-harness визуальной регрессии — детерминированные скриншоты (?visual-test=1), проверка байт-идентичности, baseline-policy владельца и обязательный negative-test допуска. Use when "добавь визуальные тесты", "настрой visual regression", "скриншот-тесты", new site scaffold testing, "сравнивать с baseline", перед первым визуальным baseline любого сайта.
---

# visual-regression-bootstrap — переносимый visual-harness

Источник: боевой harness NIKA (198/198 зелёных, все ловушки уже съедены там). Скил = один скрипт-развёртка + законсервированные уроки.

## Развёртка (одна команда)

```bash
node "%USERPROFILE%\.claude\skills\visual-regression-bootstrap\scripts\bootstrap.mjs" ^
  --project "D:\path\to\site" [--port 8788]
```

Кладёт: `tests/playwright.config.js` (workers:2, tolerance 0.0015, http-server webServer), `tests/visual/visual.spec.js` (3 viewport, scroll-through, fonts.ready), `tools/check-visual-determinism.mjs` (байт-идентичность 2 прогонов), `BASELINE-POLICY.md`; патчит `package.json` (scripts + devDeps). Существующие файлы НЕ перезаписывает (exit 2) без `--force`. Дальше — `next_steps` из вывода, по порядку.

## Обязательные гейты (порядок жёсткий)

- [ ] **Страница реализует контракт `?visual-test=1`**: `html[data-visual-test]`, canvas заморожен на детерминированном кадре (t=0/seed), все JS/CSS-анимации выключены, lazy→eager. Нет режима — сначала добавить его в страницу; harness без него бессмыслен.
- [ ] **`PLAYWRIGHT_BROWSERS_PATH=D:\playwright-browsers`** до `npx playwright install` — браузеры (~1GB) не на C: (правило владельца).
- [ ] **`npm run check:visual-determinism` → STABLE по всем viewport** до генерации baseline. UNSTABLE = чинить страницу, не допуск.
- [ ] **Initial baseline** (`test:update-baseline`) — скриншоты смотрит владелец.
- [ ] **NEGATIVE TEST — без него harness не принят:** внести видимое изменение (смена accent-цвета) → `test:visual` ОБЯЗАН покраснеть → откатить → зелёный. Допуск 0.0015 выбран именно так (0.02 глотал полный перекрас кнопки на full-page скриншоте). «Suite, который никогда не падал, ничего не доказал».

## Ловушки, уже закодированные в шаблонах (не переоткрывать)

1. `python -m http.server` не отдаёт Range → видео виснет; только `http-server` (в config).
2. CDP fullPage НЕ скроллит → lazy/reveals ниже фолда пустые; spec делает реальный scroll-through.
3. WebKit-headless не доставляет синтетические Tab/Space; `page.route` не перехватывает `.mp4` (перехватывать manifest) — для e2e-надстроек.
4. Аномалия в интерактивной Chrome-вкладке = лид для перепроверки чистым Playwright, не баг.
5. workers:2 — полный параллелизм кладёт static-server (ложные таймауты).
6. `networkidle` НЕ наступает на странице с реально загружающимся `<video>`: Chromium качает медиа чанками и держит запрос открытым. Тест умирает по таймауту, а не по ассерту — брать `waitUntil: "load"` + poll конкретного условия (`data-state`, счётчик ресурсов).
7. Byte-identity скриншотов гоняется с `--disable-gpu`: инициализация GPU-процесса даёт равномерный ±1 LSB и ложный UNSTABLE. Допуск-тесты (`toHaveScreenshot`) это поглощают — им GPU не мешает. Признак растеризатора, а не контента: diff «везде равномерно, max_delta=1, structural diff_pixels(>2)=0».

## Baseline-policy

`BASELINE-POLICY.md` кладётся в проект и является законом: обновление только владельцем через `test:update-baseline` после просмотра; красный после намеренного редизайна — корректное состояние до приёмки; изменение допуска = повторный negative-test; пойманные/пропущенные дефекты — в regression-лог (§9).
