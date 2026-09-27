# The code corpus (`.mb/code_index/`)

Singularity's third search corpus: semantic search over source files, level with
memory. It closes MB-05 (`TASK-05_Code_Indexer`, sitting at `todo` since
2026-07-02) and was implemented by the `SINGULARITY-CODE` epic (C00–C07).

**In one line:** "how did we implement retry" should be findable in the code as
fast as a decision is findable in memory — but code never reaches the results
without an explicit `--with-code`.

---

## 1. Quick start

```powershell
mb config set code_repos_dir "<path/to/your/repos>"   # the directory the repositories LIVE IN
mb index rebuild --code                               # the first build
mb search "how is retry implemented" --with-code
mb search "retry" --code-only --repos api,web
```

The path is the **parent** directory of the repositories, not one repository:
the first segment of the relative path becomes the chunk's `repo` value, which
is what `--repos` filters on. `~` is supported (`~/code`).

With `code_repos_dir` unset the corpus is off. That is the only switch; there is
deliberately no separate boolean key (decision **D1**), because a second source
of truth could drift from the first.

## 2. Commands and keys

| Command | What it does |
|---|---|
| `mb index rebuild --code` | a full build of the code corpus |
| `mb index update --code` | an increment by (mtime, size); deleted files lose their chunks |
| `mb index inspect <path> --code` | the chunks of one file in the current generation |
| `mb search <q> --with-code` | memory + code (code is NOT in the default results) |
| `mb search <q> --code-only` | code only |
| `mb search <q> --repos api,web` | filter by repository (applies to code only) |
| `mb search <q> --with-code --graph` | each code hit also says what the code graph knows: its definition, callers, callees, unplaced call sites — read-only, never refreshed; "outside graph universe" for a file no graph holds (CODE-GRAPH P7) |
| `mb search <q> --with-code --graph-boost` | re-ranks code hits by their symbol's callers in the code graph; off by default, at most 3 places, the same hits (CODE-GRAPH P7; turning it on is the owner's call, numbers in `docs/search-eval-code-v2.json`) |
| `mb nightly` | the `code_index` step catches the corpus up |
| `mb stats` / `mb status` | a code line, once the corpus is configured |
| `mb doctor` | the `K1-code-index` block: WARN/SKIP/PASS, **never FAIL** |
| `mb doctor --fix --heavy` | builds the corpus if it is configured but not built |

| Config key | Default | Meaning |
|---|---|---|
| `code_repos_dir` | `null` | the directory holding the repositories; `null` = corpus off |
| `code_index_max_age_days` | `7` | past this `mb doctor` WARNs about staleness |
| `code_skip_dirs` | `[]` | extra directory names cut at any depth |
| `code_chunk_schema` | `"v1"` | `"v1"`: the regex chunker below; `"v2"`: definition boundaries from the code graph's tree-sitter extractors (§4, "Chunker v2") |

**A repository outside the root** is attached by a link, without moving it:

```powershell
# Windows
New-Item -ItemType Junction -Path "<root>\<name>" -Target "<path to the repository>"
```
```bash
# macOS / Linux
ln -s "<path to the repository>" "<root>/<name>"
```

The repository is indexed as an ordinary child of the root, while its editable
install, its hooks and its `.venv` stay where they are. To detach it, delete the
link; no data is touched.

> **The cross-platform subtlety that makes this work.** The code corpus is
> scanned with `follow_links=True`. `os.walk` traverses a Windows junction even
> without it (Python reports `islink=False` for one), but a POSIX symlink —
> **no**: without the option a repository on macOS/Linux would silently not be
> indexed at all. Following links makes a cycle reachable (a link to an
> ancestor), so every directory is remembered by `realpath` — on Windows a cycle
> through a junction was reachable before, with no option at all.
>
> The memory and sources corpora deliberately do **not** follow links: the note
> tree is written in place, and quietly walking out through a link would index
> files the user never put in the vault.

`~` in `code_repos_dir` is expanded: `config.example.json` deliberately keeps
this key home-relative for portability.

## 3. What is indexed, what is not, and why

### Extensions (`code_chunking.CODE_PATTERNS`)

`*.py` · `*.ts` · `*.tsx` · `*.js` · `*.jsx`

`.rb` is absent **on purpose** (decision **D6**): there are zero `.rb` files in
the codebase, and a chunker for a language nothing can test it against is an
unverified guess. `.rb` falls into fallback windows and honestly does not
pretend to be supported.

Extensions are also the first layer of privacy (**D5**): `.env`, `.pem`, dumps
and keys are excluded *by construction*, not because somebody remembered to put
them in a skip list.

### Directories (`SKIP_DIRS`) — cut at any depth

`.git` `.hg` `.svn` · `node_modules` `bower_components` `vendor` `third_party` ·
`.venv` `venv` `env` `virtualenv` `site-packages` · `__pycache__`
`.pytest_cache` `.mypy_cache` `.ruff_cache` `.tox` `.eggs` · `dist` `build`
`out` `target` `.next` `.nuxt` `.svelte-kit` `.turbo` `.parcel-cache` `.cache`
`.gradle` `.terraform` · `coverage` `htmlcov` `storybook-static`
`__snapshots__` `__tests__` · `Pods` `DerivedData` `.idea` `.vscode` `.mb` ·
`_static` · `__MACOSX` (macOS zip debris: `._<name>` resource-fork twins that
carry the source file's extension — 22 of them were live chunks on 2026-09-26)

Owner-specific exclusions do **not** belong in this list. A repository name
the engine should not carry (a former employer's code, a one-off unpacked
virtualenv like `venv-capcut`) goes in the machine-local `code_skip_dirs`:
commit `6628a90` removed such a name from `SKIP_REPOS` on purpose, and the
repository quietly re-entered the index because nothing replaced it in config.

This is **the most effective filter in the whole feature** and the only one that
cuts before entering a subdirectory. Hence `_scan_corpus` moving from `rglob` to
`os.walk`: `rglob` descends into `node_modules` and throws the result away
afterwards, which is to say it pays the full price of the walk.

> `skip_dirs` in `Corpus` (the first path segment) and `prune_dirs` (a directory
> name at any depth) are different mechanisms. For a vault rooted in its own
> notes the first was enough; for a root holding 13 repositories `node_modules`
> sits at the third level, and the first mechanism never sees it.

### Files (`SKIP_FILE_PATTERNS`) — a glob over the whole relative path

`*.test.*` `*.spec.*` · `*.min.js` `*.min.css` `*.bundle.js` `*.map` ·
`*.d.ts` · `*/migrations/*` `*_pb2.py` `*_pb2_grpc.py` `*.generated.*` ·
`*secret*` `*credential*`

Matching is case-insensitive on every platform, deliberately: `fnmatch` folds
case only on Windows, and a skip rule that quietly stopped working on Linux is
exactly the class of divergence people notice six months later.

*A consequence on case-sensitive filesystems* (Linux, and macOS on a
case-sensitive volume): matching of the **including** patterns is
case-insensitive too, so `README.MD` or `Setup.PY` now reach the index where the
old `rglob("*.md")` skipped them (`pathlib` compares through `os.path.normcase`,
which on POSIX is the identity). The change moves toward "more was indexed",
which is the visible direction rather than the silent one.

### Vendored distributions (`VENDORED_DIR_PATTERN`)

```
^.+[-_]v?\d+(?:\.\d+){2,}(?:[-_.].*)?$
```

A directory whose name carries a **version of three or more numeric components**
(`renpy-8.5.2-sdk`, `node-v18.17.1-win-x64`, `python-3.11.4`, `jdk-17.0.2`) is an
unpacked third-party distribution, not hand-written code. `SKIP_DIRS` cannot
express that: the name contains a version, and versions are not enumerable.

**Why three components and not two.** Two would match `core-2.0`, `sd-1.5`,
`lr-0.001`, `api-v2` — your own code, that is. This rule errs **silently**: a
spurious exclusion removes real code and says nothing, whereas a miss is visible
in the results and fixed by one line in `code_skip_dirs`. When the two
directions of error are asymmetric, the rule must lean toward the loud one.

**Known misses** (deliberate, pinned by a test): a version separated by
underscores (`boost_1_82_0`), a single component (`node-v20`), a fused one
(`Qt5.15`). The cure is to add the directory name to `code_skip_dirs`.

**False positives are possible too**, and they have their own cure. The rule also
matches plausible names of your *own* — `schema-1.0.0`, `proto-3.21.12`,
`chart-0.1.0`. `code_skip_dirs` does not help there: it can only ADD an
exclusion, never cancel one. So the pattern itself is configurable:

| `code_vendored_pattern` value | Meaning |
|---|---|
| `null` (default) | the built-in `VENDORED_DIR_PATTERN` |
| `""` | the rule is off entirely |
| your own regex string | replaces the built-in one (validated on write) |

Diagnostics: what was cut is always named, by name, in the output of `mb index
rebuild --code`, in `mb stats` and in `tools/code_index_audit.py` — so if your
directory ended up there, you can see it rather than discover it a month later
through empty results.

The rule does **not** apply to markdown corpora: their `Corpus.vendored_dir_pattern`
is empty, so a note in a folder called `reference-1.2.3` keeps being indexed.

### Repositories (`SKIP_REPOS`)

`test-project` (decision **D1**).

Erring toward "something extra got indexed" is deliberate: that failure is
visible in the results and fixed by one line, whereas an allowlist fails
silently.

### Size

`MAX_FILE_BYTES = 100_000`. The boundary is inclusive: exactly 100 000 bytes is
indexed, 100 001 is not.

### The order of rejection is a contract, not a style

```
1. prune the directory   during the walk, the subdirectory is never opened
2. glob on the filename  a string comparison, no syscall
3. skip_dirs/skip_parts  a relative-path comparison, no syscall
4. skip_file_patterns    a regex over the relative path, no syscall
5. stat()                the only syscall, and only for the survivors
```

Swapping 4 and 5 costs one `stat()` per candidate. On the codebase survey (epic
§2) that was the difference between 504 wanted files and 56 094 candidates —
111×. The order is pinned by a test with a spy counter
(`tests/test_indexer.py::test_scan_never_stats_a_file_it_already_rejected`).

## 4. The chunker: symbol boundaries, not an AST

`singularity/search/code_chunking.py`. Line-based regex is a conscious refusal of
tree-sitter: a parser per language is a dependency, a treadmill of versions and a
hard failure on the first file that does not parse, in exchange for maybe 20%
better boundaries. Regex degrades into fixed-size windows instead of failing —
exactly the behaviour needed on a root full of unfinished drafts.

> **Reversed for the code graph, 2026-09-26 (decision CODE-GRAPH D1).** The
> "hard failure on the first file that does not parse" describes the stdlib
> `ast` module, not tree-sitter: tree-sitter recovers with ERROR nodes. On a
> 100k-file proxy it failed hard on 0 files (119 parsed partially), and on a
> deliberately broken file it still recovered 5 definitions where `ast`
> recovered 0. What survives of the refusal is the version treadmill, and it is
> answered by hash-pinned wheels and a doctor check in both interpreters.
> Regex fallback remains only for a grammar that fails to LOAD. The chunker
> itself stays on regex (schema 1) until a chunker v2 is measured against the
> code retrieval set in `tests/fixtures/search_eval.json` — the graph and the
> chunker are separate reversals.

| Constant | Value | What for |
|---|---|---|
| `MAX_BODY_LINES` | 60 | truncation of a symbol's body |
| `DOC_LOOKBACK_LINES` | 7 | a banner comment above the signature |
| `DOC_DESCRIPTION_LINES` | 4 | how many doc lines go into `description` |
| `MIN_CHUNK_CHARS` | 30 | below this it is a stub or a re-export, not a symbol |
| `FALLBACK_WINDOW_LINES` | 40 | the window when there are no boundaries at all |
| `FALLBACK_OVERLAP_LINES` | 10 | overlap between windows |
| `FALLBACK_MIN_CHARS` | 50 | the minimum for a window |
| `EMBED_CODE_CHARS` | 250 | how much of the body reaches the embedding |
| `CODE_CHUNK_SCHEMA_VERSION` | 1 | the chunk format version of **this** corpus |

**Python is the main path** (69% of the codebase, decision D6) and entirely new
code: in the predecessor `.py` went through the TS chunker and almost always fell
into fallback. Implemented: `def` / `async def` / `class`, a method inside a class
as its own chunk, decorators pulled into the chunk below them (`@app.route("/orders")`
is the single most-searched fact about a handler), the docstring read **forward**
from the signature (including a multi-line one), and a `#` banner read backward.

**TS/JS/JSX** — ported boundaries: `class` / `interface` / `function` / `enum` and
`export const|let|var` (which covers both arrow functions and configuration
objects — a module's public surface).

Chunk fields: `file`, `repo`, `kind`, `name`, `description`, `content`,
`line_start`, `line_end`, `chunk_id`, `text`. The names were not chosen afresh:
`engine._compact_line` already reads `kind`/`name`, and `_match_filters` reads
`repo`. The reading half was built ahead of time in W03, deliberately.

Two conscious departures from the letter of the spec:

1. What is embedded is `"{file} — {kind} {name}: {doc} | {code[:250]}"`, and
   **not** `"{repo}/{file} — …"`: `file` here is relative to the corpus root and
   already begins with the repository segment (the key is formed by
   `_scan_corpus`, and `_update` computes the keep-mask from the same one), so a
   leading `repo` would give `api/api/app.py`.
2. `line_end` is the last **included** line after the body was truncated, not the
   real end of the symbol. That keeps the pair `(line_start, line_end)`
   verifiable: reading those lines from the file reproduces `content` — which is
   what the test checks.

**Known limitation:** a file's preamble (the module docstring, the imports) does
not become a chunk — a chunk starts at the first symbol boundary. A file with no
boundaries at all (a config object, a bare script) goes entirely into fallback
windows.

### Chunker v2 (CODE-GRAPH P7) — opt-in, `code_chunk_schema: "v2"`

`singularity/search/code_chunking_v2.py`. The refusal above was reopened by
decision CODE-GRAPH D1: tree-sitter RECOVERS from a broken file (0 hard failures
and 119 partial parses on 100k files), and the version treadmill is closed by
pins. v2 chunks at definition boundaries using the code graph's extractors (one
chunk per module-level or class-level definition, `qname` in the metadata),
keeps the preamble v1 drops, cuts long definitions into overlapping windows and
merges fragments under 30 characters into a neighbour, so no text is lost.

- **Chosen by config, never by the parser being installed.** v1 stays the default
  and stays byte-identical (a golden test pins it).
- **Pins, not the loaded wheels.** The index is stamped with the grammar versions
  pinned in `requirements-graph.txt`. A writer whose loaded grammars differ, or
  cannot load them, refuses (`GrammarPinRefused`, a WARN, exit 2 from
  `mb index --code`) and leaves the code `CURRENT` untouched — no silent fall back
  to v1. A pin change rebuilds the code corpus, and only it: memory's `CURRENT` is
  byte-identical across a v1 -> v2 -> v1 round trip.
- **The gate, measured on an isolated copy** (`docs/search-eval-code-v2.json`, 20
  code cases, fusion on): R@5 .55 -> .75, R@10 .55 -> .80, MRR .442 -> .508, two
  cases worse by a rank or three, six better. Cost: +48% chunks, +40% build time.
- **Before switching it on:** v2 indexes text v1 never did (preambles), and the
  D5 layer-3 count rose from 24 to 38 secret-shaped markers on the owner's root
  (test fixtures of `redact`, integration harnesses, one module-level literal).
  That is the owner's review, not a default.

## 5. Numbers on a real codebase (C06)

Taken 2026-07-29 on a real repository root (13 repositories of a mixed stack:
Python tools, ComfyUI nodes, several frontends, one project with a vendored
engine). Windows 11 Pro 26200, encoder on CPU.

| Metric | Value |
|---|---|
| Repositories | 13 |
| Files indexed | **440** |
| Chunks | **3666** (memory for comparison: 3216 → a ratio of **1.1×**) |
| Embeddings | 5.6 MB float32 |
| `metadata.json` | 4.6 MB |
| Full build (cold daemon start + model load + 3666 embeddings) | **71.4 s** |
| `mb index update --code` with no changes | **0.5 s** |
| Cut as vendored | `renpy-novel/renpy-8.5.2-sdk` |
| Warm `mb search` with no flags | 13.5–18.1 ms (mean 15.3) |
| Warm `mb search --with-code` | 14.6–21.1 ms (mean 16.8) |

**The fallback share is 1.7%.** That is the chunker's main quality figure: in
98.3% of cases the regex found a real symbol boundary rather than giving up into
windows. Risk R3 ("junk chunks on unfamiliar styles") did not materialise on this
codebase.

**The budget was not exceeded.** The filters were narrowed because of the
composition, not because of the budget: see §7.

### Privacy: checking a fact, not a hypothesis (D5, layer 3)

A grep over the built corpus's `metadata.json`:

| Marker | Found |
|---|---|
| `AKIA[0-9A-Z]{16}` | 0 |
| `-----BEGIN … PRIVATE KEY-----` | 0 |
| `password = "…"` | 0 |
| `api_key` / `secret` / `token = "…"` | 0 |
| `Bearer <20+ characters>` | 0 |
| `sk-…` / `ghp_…` | 0 |
| **Total** | **0** |

Files with the extensions `.env` / `.pem` / `.key` / `.p12` / `.pfx` / `.sql` /
`.dump` in the index: **0**. Files with `secret`/`credential` in their path:
**0**.

The first two layers (extensions, skip patterns) are hypotheses about where
secrets live. This third one is the only one that checks a fact:

```powershell
python tools/code_index_audit.py          # exit 1 if even one marker is found
python tools/code_index_audit.py --json
```

It also prints the corpus composition — the fallback share and the share of the
largest repository (see §7: a share above 50% almost always means a vendored
dependency slipped past `SKIP_DIRS`).

### Isolation from memory

`memory_index/CURRENT` = `gen_1785272708380_4116_000032` **before and after** both
full builds of the code corpus. No code operation touched the memory generation.
Gate C01 (an upgrade must not force a rebuild) was checked separately: `mb curator
--json` and `mb digest plan --json` matched byte for byte by SHA-256
(`D956B408…`, `A59CA966…`).

## 6. Decision D2, by the numbers

D2 made code opt-in (`--with-code`) with an explicit caveat: revisit it on the
C06 numbers. The numbers were taken — the same 10 control queries from the
[baseline](code-index-baseline.md), one session, one variable:

The measurement was taken **twice**, and the second run overturned the
conclusion of the first. That is the section's main result.

| Q | \| code hits \| C06 → C10 | \| displaced \| C06 → C10 | top-1 changed, C06 → C10 |
|---|---|---|---|
| Q01 | 0 → 2 | 0 → 2 | no → **yes** |
| Q02 | 1 → 1 | 0 → 0 | no → no |
| Q03 | 3 → 3 | 0 → 0 | no → no |
| Q04 | 0 → **5** | 0 → **5** | no → **yes** |
| Q05 | 0 → 1 | 0 → 1 | no → **yes** |
| Q06 | 0 → 0 | 0 → 0 | no → no |
| Q07 | 1 → 1 | 1 → 1 | no → no |
| Q08 | 1 → 1 | 0 → 1 | no → no |
| Q09 | 3 → 2 | 2 → 1 | no → **yes** |
| Q10 | 1 → 2 | 1 → 2 | no → no |
| **total** | **10 → 18** / 50 | **4 → 13** / 50 | **0 → 4** / 10 |

**The first measurement (C06)** — a corpus of 5775 chunks, 83% of which came
from a vendored third-party SDK. Displacement of 8% of the slots, and top-1 never
changed. The conclusion then: the R1 fear was not confirmed, and the numbers
permit turning the merge back on by default.

**The second measurement (after C09/C10)** — the same tool, the same 10 queries,
a corpus of 3666 chunks: the third-party SDK was cut, but Singularity's own code
came in (204 files). Displacement **tripled**, and top-1 changed on 4 queries out
of 10. Q04 ("atomic commit of index generations") gave 5 code hits out of 5 and
pushed the entire memory top-5 out.

**Why, and why this is NOT a degradation.** Q04 is now answered by
`_commit_generation` — the real implementation instead of a note about it. The
answer got better; what changed is the price of the question. The code corpus
displaces memory exactly in proportion to how relevant the code is to the query,
and relevance jumped sharply the moment **one** substantial first-party
repository entered the corpus.

**The consequence for D2 is the opposite of what the first measurement showed.**
The argument "displacement is small, the merge can be on by default" rested on
the corpus's composition, not on a property of the system: it collapsed when one
repository was added. The owner kept opt-in with exactly this reasoning — "there
is little code now, but on large products this will play a nasty trick" — and the
second measurement confirmed it with figures. **D2 stands: code only with
`--with-code`.**

The moral of the measurement: one-off displacement figures measure today's corpus
composition, not the system. Re-check after every change of composition —
`python tools/code_index_baseline.py capture --both && ... compare`.

### Third measurement, 2026-08-08 — and what of it is comparable

`docs/code-index-baseline-2026-08-08.json` (`capture --both`, daemon restarted
first so it served the current modules). The C00 file is NOT overwritten: it
records a state that cannot be reproduced, since the corpus exists now.

| | C06 | C10 (v0.5.0) | 2026-08-08 |
|---|---|---|---|
| code hits in top-5 | 10 / 50 | 18 / 50 | **29 / 50** |
| notes displaced | 4 / 50 | 13 / 50 | 24 / 50 |
| top-1 changed | 0 / 10 | 4 / 10 | 9 / 10 |

**Two variables moved at once, so the row was checked before it was believed.**
This run's ten questions were re-worded into English while the vault is not; the
code is. A wording that favours the code corpus would inflate every number here
for a reason that has nothing to do with the corpus.

Re-measured with the ORIGINAL wording, same corpus, same session, one variable:

| | English wording | original wording |
|---|---|---|
| code hits in top-5 | 29 / 50 | **29 / 50** |
| notes displaced | 24 / 50 | 21 / 50 |
| top-1 changed | 9 / 10 | **5 / 10** |

So: **code hits are robust to the wording** and the rise from 18 to 29 is the
corpus, which is a real result and the one D2 is argued on. **top-1 is not** —
9/10 against the earlier 4/10 compares two different question sets and means
nothing. Displacement sits in between and should be read as "roughly a fifth to
a half", not as a trend line.

D2 is untouched by this: every number moved in the direction the owner's
reasoning already predicted ("there is little code now, but on large products
this will play a nasty trick"), and the decision was never about the size of the
effect.

Gate C04 separately: **the default `mb search` results with no flags matched the
C00 baseline on all 10 queries, 0/10 divergences** (`chunk_id` + `score` to 4
places). Under opt-in that has to hold strictly — any divergence would be a
plumbing bug.

> **An open question for the owner.** The numbers permit switching the default to
> a merge. The decision does not change automatically: D2 chose its side not by
> volume but by the asymmetry of reversibility (opt-in → merge is cheap, the
> other way means breaking an established habit and other people's scripts).
> Changing the default is a separate decision by the owner.

## 7. C06 observations that need an owner decision

C06 found two things invisible on fixtures — which is exactly why it exists as a
separate task. **Both are closed**, and both turned out to be owner decisions
rather than bugs.

**1. The tool's own repository never reached the corpus.** It lay outside
`code_repos_dir`, and there is exactly one root. The query "how did we implement
atomic write" found anything except its own code.

*Closed with a junction* (see §2): the repository stays where it is but is
visible from the root as an ordinary child. Moving the folder was rejected after
a check — it breaks the editable install (`mb` stops importing), four Claude Code
hooks with absolute paths, and a `.venv` with the old path baked in. A
multi-root `code_repos_dir` was rejected too: adversarial review showed it breaks
`SKIP_REPOS` and `*/migrations/*` (the repository name moves out of `sub` into
the prefix), requires a schema migration, and cannot be expressed through
`mb config set` on Windows. A junction gives the same result in one command and
without a single line of code.

**2. One repository with a vendored engine produced 83% of the corpus.** A
project with an unpacked third-party engine SDK inside it (4776 chunks out of
5775) dominated the results: on a code-oriented probe 5 of the first 10 hits came
from there — for example an animation-wait method for a query about retry
backoff.

*Closed by the `VENDORED_DIR_PATTERN` rule* (see §3): a directory with a version
of ≥3 numeric components is cut at any depth, and what was cut is **named by
name** in `mb index rebuild --code`, in `mb stats`, in `metadata.json` and in the
audit. A counter would not have been enough: it does not distinguish "somebody
else's engine was cut" from "your snapshot was cut", which satisfies the letter of
the rule against silent exclusions and not its meaning.

Composition diagnostics in one command:

```powershell
python tools/code_index_audit.py     # the `largest repo` line
```

A share above 50% for one repository is a signal, not a verdict: a vendored
dependency has the same share as your main repository would. On this machine,
after both fixes, the largest became Singularity itself (72.7%) — which is the
right outcome, not a finding.

## 8. The flow

```text
title Indexing and searching code

participant U as "User"
participant CLI as "mb CLI"
participant D as "search daemon"
participant Corp as "Corpus(code)"
participant Chunk as "code_chunking"
participant Idx as ".mb/code_index/"
participant Eng as "SearchEngine"

U->CLI: mb index rebuild --code
CLI->CLI: is code_repos_dir set? no -> exit 2 with a hint
CLI->D: {"cmd":"index","action":"rebuild","corpus":"code"}
D->D: the same gate again (the single point through which\nCLI, nightly and doctor --fix all pass)
D->Corp: resolve_corpus(cfg, "code")
Corp->Corp: _writer_lock("code") -- does NOT block memory
Corp->Corp: _scan_corpus: prune SKIP_DIRS -> glob CODE_PATTERNS ->\nskip_dirs=SKIP_REPOS -> SKIP_FILE_PATTERNS -> stat()
loop over every surviving file
Corp->Chunk: extract_code_chunks(path, rel)
Chunk->Chunk: regex boundaries; decorators pulled up, docstring read forward (Python)
Chunk->Chunk: truncate at 60 lines; no boundaries -> windows 40/10
Chunk-->Corp: chunks[{repo,file,kind,name,description,\nline_start,line_end,chunk_id,text,content}]
end
Corp->Idx: encode -> _commit_generation(chunk_schema=CODE_CHUNK_SCHEMA_VERSION)
Idx->Idx: gen_<token>/ + summary.json, then an atomic swap of CURRENT
D->Eng: engine.reload() -- checks the model against memory

== search ==
U->CLI: mb search "how does retry work" --with-code --repos api
CLI->D: payload include_code / repos
D->Eng: search(req)
Eng->Eng: memory always; code only under include_code|code_only
Eng->Eng: repos applies only to chunks that HAVE a repo
Eng->Eng: merge by score -> max_per_file -> strict top_k
Eng-->U: "api/retry.py | 0.61 | [function] | retry_with_backoff | code…"
```

## 9. What this corpus does NOT do

* **it has no watcher** (decision **D4**). Freshness is kept by the `code_index`
  step in `mb nightly` and by a manual `mb index update --code`. `mb doctor`
  WARNs when the corpus is older than `code_index_max_age_days`;
* **it takes no part in memory analytics**: `mb curator`, `mb digest` and
  `mb ingest plan` cannot see it at all — different index directories and
  different generations;
* **it does not parse an AST** — see §4;
* **it cannot fail `mb doctor`**: the `K1-code-index` block never emits FAIL in
  any scenario. `.mb/code_index/` is fully recoverable by a rebuild, unlike
  `.mb/sources/`, where data loss is irreversible.

## 10. Rollback

Fully described in [ROLLBACK.md](ROLLBACK.md). In short:

```powershell
mb config set code_repos_dir null      # the corpus is off, search is memory-only again
Remove-Item -Recurse "<vault>/.mb/code_index"   # optional: reclaim ~30 MB
```

No note is touched by any of this.
