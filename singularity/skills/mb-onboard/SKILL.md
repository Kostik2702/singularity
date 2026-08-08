---
name: mb-onboard
description: Register a new project in the Singularity vault and give it a persistent SessionStart brain note. Use when the user says onboard this project, "add this project to memory", "connect this repository to the memory", or starts working in a codebase that mb project detect doesn't yet recognize.
---

# mb-onboard — registering a new project

Through the sanctioned CLI (`mb project`, `mb note new`) plus a manual edit of `.mb/context.json` (which has no CLI command of its own yet — use Edit/Write). Never use fork-only paths of the form `code/...`; a project is what is registered in the registry, not what happens to sit in some particular directory.

## Protocol

- [ ] 1. Decide the `<id>` (short, stable, no spaces), the path to the project, and optionally `--group`/`--about`.
- [ ] 2. Register it: `mb project add <id> <path> [--group G] [--about "..."]`. If the id is taken or the path overlaps another project, `mb project list` will show the conflict — do not force an overwrite.
- [ ] 3. Create the starter brain note: `mb note new reference "<Project> Brain"` — briefly (the project's purpose, key paths and commands, architectural decisions the agent must know in EVERY session of this project). This is not the project's documentation in full — only what is worth the `brain_max_lines` budget (40 lines by default, `mb config get brain_max_lines`).
- [ ] 4. Write the bridge into `.mb/context.json` (Edit/Write, atomically — read the file, extend the object, do not overwrite other projects): `{"<id>": {"brain_note": "<vault-relative path to the note from step 3>"}}`. Without this step SessionStart will not find the note — `mb project add` registers ONLY the project's path, not the brain note.
- [ ] 5. Verify: `mb project detect <project path>` must return `<id>`; the next session in that directory should show "Project brain (`<id>`)" in the SessionStart context.
- [ ] 6. Tell the user in one line: the project is registered, the brain note is created, and what is worth adding to it over time.

## Do not

- [ ] Do not create the project ids `general`/`personal` — they are reserved (RESERVED_IDS) and `mb project add` will reject them.
- [ ] Do not write to `.mb/context.json` without reading the current contents first — it is a shared file, and an overwrite erases other projects.
- [ ] Module context (`context_map` inside a project's entry, per-module notes) is a separate and rarer setting; do not add it proactively, only when the user explicitly asks for per-module context.
