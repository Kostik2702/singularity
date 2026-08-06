---
type: reference
title: "Phase 0 — Выбор подхода"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 0 — Выбор подхода

Родитель: `_router.md`.

- [ ] Выбери маршрут по категории из `phase-SB-step-back.md`:
  - **research / незнакомая область** → Phase 1 Investigation (глубокое чтение кода+памяти).
  - **известный bug** → systematic-debugging (гипотеза → минимальная проверка → binary search), затем Phase 3.
  - **сложный bug / странное поведение** → Investigation с falsification-протоколом (каждую гипотезу пытайся ОПРОВЕРГНУТЬ).
  - **архитектурное решение** → Phase 1.5 grill → design-заметка (type=decision) с альтернативами.
  - **S-размер механика** → сразу Phase 3 (пометь `[➖] phases 1-2 — trivial scope`).
- [ ] Выбор маршрута = задачевое решение → зафиксируй в learnings (кто/почему; соло-дефолт: `ASSUMED route X from category+size`).

**Выход:** маршрут записан → соответствующая фаза.
