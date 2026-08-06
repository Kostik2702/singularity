---
type: reference
title: "Phase 3 — Implementation"
tags: [workflow, phase, singularity]
created: 2026-07-03
status: active
---

# Phase 3 — Implementation

Родитель: `_router.md`.

- [ ] Загрузи правила репо (его CLAUDE.md / conventions) и [[Core Discipline - Checkbox Gates]]; пиши в идиомах окружающего кода.
- [ ] Read-before-edit: файл прочитан перед правкой; правка минимальна по scope плана.
- [ ] Ни одного молчаливого изменения бизнес-логики сверх плана — заметил необходимость → стоп, зафиксируй решение.
- [ ] Verification-after-edit (§17 Genevra): после «готово» — `git diff --stat` и глазами: изменено ровно то, что планировалось?
- [ ] Деструктивные операции (sed -i, перезапись) — только с бэкапом/через tmp-swap.
- [ ] Кроссплатформенные гейты кода (для Singularity-кода): [[05_Cross_Platform_Strategy]] §4.
- [ ] Авто-append: что изменено (file:line), отклонения от плана и почему.

**Выход:** диф соответствует плану → `phase-4-review.md`.
