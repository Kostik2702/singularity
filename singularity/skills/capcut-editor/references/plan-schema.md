# capcut-plan/1

capcut-plan.json — агентский план и evidence anchor. Это не внутренний формат
CapCut и не должен копироваться в draft. Все времена — секунды, пути — абсолютные.

Example:

    {
      "schema": "capcut-plan/1",
      "project": {
        "name": "promo-ai-20260731-180000",
        "mode": "new",
        "source_draft": null,
        "draft_root": "D:/CapCutDrafts",
        "canvas": [1080, 1920],
        "fps": 30,
        "target_duration": 20.0,
        "media_policy": "link"
      },
      "backend": {
        "server": "capcut",
        "profile": "general-draft",
        "version": null,
        "evidence_level": "B"
      },
      "tracks": [
        {
          "id": "V1",
          "kind": "video",
          "items": [
            {
              "source": "D:/media/shot-01.mp4",
              "source_in": 1.2,
              "timeline_in": 0.0,
              "duration": 3.0,
              "fit": "crop",
              "volume": 0.0
            }
          ]
        }
      ],
      "text": [],
      "captions": [],
      "effects": [],
      "keyframes": [],
      "approvals": {
        "canvas_and_crop": false,
        "copy": false,
        "existing_draft_clone": true
      }
    }

## Invariants

- [ ] schema строго capcut-plan/1.
- [ ] mode — new или clone; при clone задан source_draft.
- [ ] draft_root разрешён в абсолютный путь; media_policy — link или copy.
- [ ] canvas и fps положительные; все временные поля конечные и неотрицательные.
- [ ] Все source/timeline times квантуются на целый кадр project fps; readback-расхождение не превышает 0.5 кадра.
- [ ] Каждый source существует до первого MCP write.
- [ ] source_in + duration не выходит за длительность media.
- [ ] Items одного track не перекрываются, если overlap не выражен явным transition/effect.
- [ ] Ни один requested text/caption/effect/keyframe не остаётся без соответствующей MCP capability.
- [ ] При media_policy=copy доказана import/copy capability; при link внешние абсолютные зависимости попадут в handoff.
- [ ] Все обязательные approvals=true до первого MCP write.
- [ ] Plan не содержит секретов, токенов и удалённых media URL без явного разрешения.

## Maintenance gate

- [ ] После изменения примера извлечь его как отдельный JSON-документ и успешно распарсить стандартным JSON-парсером. quick_validate.py этот класс ошибок не проверяет.

При расхождении plan и фактического tool schema адаптировать только mapping вызова,
не менять смысл plan молча.
