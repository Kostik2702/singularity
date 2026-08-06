---
type: reference
title: "Phase 1 — Investigation"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 1 — Investigation

Родитель: `_router.md`.

- [ ] SEM-FIRST: `mb search` по концепции, затем целевые Read по найденным file:line. Не читай файлы «на всякий случай».
- [ ] Gap-analysis: чего я НЕ знаю для решения? Каждый gap — закрыт (evidence) или явно OPEN.
- [ ] Verify code first: каждое утверждение о поведении кода — подтверждено чтением кода (file:line), не памятью и не предположением.
- [ ] Disproval-first: сформулировав вывод — попытайся его опровергнуть (контрпример, соседний код-путь, edge case). Не смог — вывод жив.
- [ ] Root-cause вопрос ×3 (для багов): «это ДЕЙСТВИТЕЛЬНО корневая причина, а не симптом?»
- [ ] Уверенность в выводе — через skill `confidence-calibration` (никогда на глаз).
- [ ] Авто-append в learnings: находки (file:line), закрытые/открытые gaps, root-cause гипотеза + %.

**Выход:** выводы с evidence и калиброванным % → `phase-1-5-grill.md` (если дальше план/дизайн) или `phase-3-implementation.md`.
