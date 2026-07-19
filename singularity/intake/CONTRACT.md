# component-intake — quarantine contract (component-intake/1, WSF-E03)

Proves an external candidate's fitness WITHOUT letting it touch the project
or execute. v1 is deliberately **inspect-first only**: no npm install, no
lifecycle scripts EVER, no candidate code execution of any kind. Runtime
phases (usage fixture, Playwright evidence, bundle delta) are v2 and are
reported as typed `unavailable`, never silently passed.

## §1 Envelope & exits

`{"schema_version": "component-intake/1", command, status, tool, inputs,
data, verdict, outputs, error}`. `run` is evaluative three-way:

| exit | meaning |
|---|---|
| 0 | verdict **PASS** |
| 1 | verdict **ADAPT** or **REJECT** (see `verdict.result`) |
| 2 | tool error — never a domain verdict |

## §2 Phases (each typed in `data.checks`, `ok|fail|unavailable`)

1. **workspace** — disposable dir; refused inside the engine repo, any
   `INTAKE_PROTECTED_ROOTS` root, or the source project. All writes atomic.
2. **fetch** — candidate source WITHOUT execution: shadcn registry item JSON
   (inline `files[].content`) via HTTP, or `--source-file` (offline/tests).
   Redirect targets and sizes bounded by scout fetch guards.
3. **snapshot** — sha256 per file + manifest (retrieved_at, source url,
   revision if present) — provenance is mandatory evidence.
4. **license** — classifier: explicit E02 candidate record (preferred,
   provenance chain) > LICENSE file in payload > SPDX headers. `unknown` =>
   REJECT, no exceptions (decision §4).
5. **scripts** — package.json lifecycle scripts (`postinstall`, `preinstall`,
   `prepare`, `install`) => REJECT. Scripts are never executed to find out.
6. **static-scan** — per file: `eval`/`new Function` => REJECT;
   network calls (fetch/XHR/WebSocket/http(s) URLs) outside the allowlist
   (`fonts.googleapis.com`, same-source CDN) => REJECT (hidden runtime
   fetch); `document.cookie`/`localStorage` => risk note (ADAPT).
7. **deps** — declared dependencies reviewed: heavy engines
   (three/gsap/motion) => lazy-island requirement (ADAPT); mixed animation
   engines in one candidate => risk.
8. **build** — syntax/transform proof via esbuild when available
   (`INTAKE_ESBUILD` or PATH); transform failure => REJECT. esbuild absent
   => `unavailable` AND the final verdict is CAPPED AT ADAPT — a tool
   failure can never produce PASS (E06 principle).
9. **reduced-motion** — animating code (rAF / CSS animation / transition)
   without any `prefers-reduced-motion` handling => ADAPT requirement.
10. **verdict** — REJECT if any REJECT-class finding; else ADAPT if any
    ADAPT-class requirement or any check `unavailable`; else PASS.
    `data.requirements` lists what adaptation must fix (E04 input).

## §3 Filesystem / secrets

Workspace only; the tool cannot write into protected roots by construction.
Subprocesses (esbuild only) run with a scrubbed environment (PATH,
SystemRoot, TEMP) — no tokens/secrets inherited.

## §4 Amendments

None. Changes require an owner-visible commit touching this file.
