---
name: create-image-prompt
description: Create or refine detailed image-generation prompts for photorealistic, cinematic, editorial, product, architectural, landscape, character, or stylised imagery. Use when a user asks for an image prompt, prompt detail, a negative prompt, a prompt variant, or a prompt for an image edit.
---

# Create Image Prompt

## Workflow

Write a ready-to-copy prompt in the user's language. Retain their creative intent and choose details that serve it.

1. Establish the subject and its concrete attributes; for people, use an adult subject unless the user explicitly indicates another safe, appropriate age.
2. State action/pose, setting, composition/camera viewpoint, lighting and atmosphere.
3. Add material, texture, physical-continuity and genre details that make the scene believable.
4. For edits, state both the desired change and invariants to preserve: identity, proportions, perspective, material behaviour, texture, lighting and shadows.
5. Provide a negative prompt only when the target generator supports it or when it would materially improve the result.

## Prompt structure

Use this order when it fits the request:

`[medium/genre] of [subject] [action/pose] in [environment], [wardrobe/props/materials], [composition/camera], lit by [source, direction, softness, colour], [atmosphere and background], [realism/style controls]`

Use only relevant detail. Do not invent a brand, artist, setting constraint or camera specification that conflicts with the request. Avoid empty quality labels when a visual fact is available.

## Self-contained prompts (mandatory)

Every main prompt and negative prompt must stand alone. The user pastes it into a generator with no chat history.

- **Never** put meta-references to earlier prompts, turns, or outfits inside the prompt text.
- **Forbidden in prompt body** (examples, not exhaustive): `different from…`, `not the previous…`, `same as before`, `as above`, `unlike the last look`, `next beat of the same…`, `continuing from…`, `(see previous)`, comparisons to a prior wardrobe/setpiece the generator cannot see.
- **Describe only what is in THIS frame.** Do not steer by negating other locations or setups the model should “avoid” in prose (`not at the campfire`, `away from the fire circle`, `not sitting with the group`, `instead of the tent`, `no longer at the car`). Those tokens still pull the discarded scene into the image. Pick one place and action; write only that place and action.
- Serial scenes (games, story beats, outfit swaps): each challenge/beat is a **full new scene**—location, people, action, wardrobe, light—rewritten from zero. Human labels outside the fence may say “next challenge”; the fenced prompt must not.
- If the user asks for a variation, **restate** the full scene and the new wardrobe/pose/lighting in full. Continuity lives only in what you rewrite, not in pointers to old text.
- Short labels outside the fenced block (for the human) are fine; the fenced prompt itself must not depend on them.

## Output

- Give the main prompt in a fenced `text` block.
- Briefly call out only the most useful controllable variables when the user asks about detail or wants iteration.
- For generators with negative prompts, add a separate concise block. Tailor it to likely defects instead of using a long generic list.
- When essential information is missing, make safe genre-appropriate assumptions and state them in one short sentence; ask a question only if the missing choice would materially change the image.
