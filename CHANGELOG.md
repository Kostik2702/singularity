# Changelog

This file summarizes the `SINGULARITY_INCOMING_FORK` epic (T00–T16): porting
a user-authored fork's product contracts into current upstream Singularity.
The fork's own source was confirmed unavailable (T00) — everything below was
authored fresh against the current codebase from the fork's *behavioral*
specification, not ported line-for-line. Full task-by-task detail, decisions,
and real-machine verification evidence live in the project vault; this file
is the repo-facing summary. Commit hashes refer to `master`.

---

# v0.12.0 — reel removed: the spare path that decided the tool

`singularity/reel` was the Python predecessor of `video-engine` — a whole
editing pipeline (`build`, `bed`, `mux`, `qa`, `beats`, `sections`, `match`,
`probe`, `sheets`) written before the engine existed, and kept afterwards as a
spare path.

The spare path is what made the decision. Asked for a montage, the tool that
gets chosen is the one that runs, not the one the architecture points at: a
full-length clip was assembled with `reel` while the owner had twice named
`video-engine` as the target. The map of the video system lists three tools —
`capcut-bridge`, `video-engine`, `video-effects-motion-service` — and `reel` was
in none of them. So it is gone, along with its console script, shims, tests and
the `reel-editor` skill.

**Removed:** the `reel` package (14 modules), `tests/test_reel.py`, `bin/reel`,
`bin/reel.cmd`, the `reel` entry point and its ruff exemption.

**Where the capability went**, stated rather than assumed:

| was | now |
|---|---|
| `build` — crop, scale, zoom, speed | `video-engine render` |
| `bed` + `mux` — the music under a cut | `video-engine`, the timeline carries `audio` |
| `beats` / `sections` — tempo, energy map | `capcut-bridge tempo --sections` |
| `probe` / `sheets` — intake, contact sheets | `capcut-bridge intake` / `survey` |
| `match` — colour drift across clips | **not covered** — engine work, filed |
| look / grain / vignette / dissolve | **not covered** — same |
| `qa` — WCAG flash count on the rendered file | **not covered** — the only gate here with a medical boundary |

The first four rows are a straight win. The last three are the reason this is a
minor bump and not a patch: until the engine grows them, a clip it renders is
flat and its photosensitivity is unverified on the file itself.

Verified: `mb doctor` 38 PASS after reinstall, `reel` resolves to nothing.

---

# v0.11.2 — the sentence that read as evidence

`singularity/search/lexical.py` cited `tests/fixtures/search_eval.json` twice
as the reason BM25's k1/b and RRF's k stay out of config: "tuning them without
an evaluation set is guessing, and the evaluation set is small enough that any
change it cannot justify should not be made."

The file did not exist. Not in the repo, not in git history, referenced by
nothing but that docstring. For a year the sentence read as evidence and was
none — the same class as a green test that measured nothing, in a form the
suite cannot catch.

### The set

66 query → note pairs, plus 16 for the code corpus, scored separately because
averaging two corpora would hide which one a change moved. Quotas per class —
22 identifier, 22 natural, 22 cross-lingual — so a change that helps exact
tokens and hurts cross-lingual cannot report "no effect". Every target was
chosen FIRST, by reading the note; the query was written afterwards, so the set
can report a bad number. Part of the queries are verbatim `mb search` calls
lifted from real transcripts.

First honest reading of this vault: **recall@1 0.485, recall@5 0.712, MRR
0.585.** Cross-lingual — the claim the multilingual model exists for — was the
weakest class and had never been measured at all.

### What it settled, and what it refused

- **Hybrid on by default**: +0.060 recall@1, +0.030 recall@5, +0.041 MRR. The
  cost is measured and accepted, not absent: cross-lingual loses 0.091
  recall@5, because BM25 has nothing to say when the query and the note are in
  different languages and RRF still gives its ranking a vote. Four damping
  variants were swept — capped lexical candidates, down-weighted fusion,
  reserved slots — and every one gave back more than it recovered.
- **The code corpus fuses unconditionally**, past the machine setting: +0.125
  recall@1, +0.083 MRR, and there not one class moves down. Half of what a code
  corpus is for is answering `atomic_write_json`, which is the query an
  embedding is worst at.
- **A usage tie-break was implemented and removed by its own measurement.** At
  a wide band it cost 0.015 recall@1; at a narrow one it changed nothing. With
  usage known for 15 notes of 556 the signal is too sparse to arbitrate — and
  sparse data made it harmful, not neutral. The counting stays; the ranking
  change did not ship.

### Four defects it uncovered

- **A warm daemon serves the modules it started with.** `reload` re-reads the
  config and the indexes, never the code. The first eval run reported a
  dead-flat zero delta on all three classes to three decimals — not because
  fusion does nothing, but because the branch had never been loaded. Recorded,
  that run would have left "hybrid does not help" under `docs/` permanently,
  with numbers, reproducibly. `machine/code_version.py` fingerprints the source
  tree; `mb status` and `B7-daemon` report the gap; "unknown" is deliberately
  not "stale", because a constant warning is an ignored one.
- **The lexical cache was never invalidated.** `SearchEngine._bm25` survived
  `reload()` and was keyed on chunk COUNT, so a note edited without gaining or
  losing a section left BM25 matching text that no longer existed — invisible,
  because the vector half went on answering correctly.
- **A config value could not be pinned.** `save()` dropped any key equal to
  `DEFAULT_CONFIG`, so while `hybrid_search` defaulted to false, setting it to
  false deleted the key. No install could have opted out of the flip, and the
  v0.6.3 promise about `vault_guard_enabled` ("an install that has ever saved
  its config carries an explicit value") was false for every sparse config.
  Fixed; README corrected rather than quietly left standing.
- **The budget notice named neither scale nor remedy.** It said something was
  truncated, which is why nobody ever acted on it.

### The write path

- **`knowledge/redact.py`** masks known secret shapes before anything reaches
  the vault — a git repository, where a key is in history on every machine the
  vault ever reached, which is why this runs on write and not as a later audit.
  Replace, not refuse: `sk-XXXXXXXX` keeps the type marker so a later reader
  knows what was here, and the insight survives with one token gone. The notice
  is derived from what ACTUALLY changed, so a dropped assignment goes quiet
  instead of announcing a redaction that did not happen.
- **`mb note split`** turns a note past `brain_max_lines` into an index plus one
  child per `##` section, linked both ways, all-or-nothing on write. Truncation
  was the wrong remedy: the tail of a brain note is the newest part, not the
  least important.
- **`mb journey`** answers "what did we work out last month" from two sources —
  notes by `created:` (never mtime, which floats every touched file to the top)
  and learnings by timestamped section (never by file, which says nothing after
  months of appends).
- **`.mb/usage.json`** counts note opens from the transcript walk `mb reflect`
  already performed and discarded. Accumulating, because transcripts are pruned
  by their clients and a recount would silently zero a note's history. Applied
  only where it earns its keep: `mb curator` reports stale AND never-opened, a
  far smaller set than either signal alone.
- **`mb sessions`** — an opt-in FTS5 store over your own past sessions, the one
  feature here that keeps conversation text. Off by default, its own database,
  redacted on the way in, finite retention, `mb sessions purge`. Claude
  transcripts only, and both `mb sessions status` and `B6b-sessions` say so
  rather than letting "sessions are searchable" stand with two of four clients
  silently absent. The contract was written in the vault before the code.

Every invariant above was proven the way this project requires: by planting its
violation, watching the suite go red, and restoring it.

Full suite: 2 066 passed, 25 skipped — the skip count unchanged, which is the
check that no check switched itself off. ruff at zero. `mb doctor` PASS 38.

---

# v0.11.1 — the verification rule reaches the thread that broke it

A follow-up, and the reason is worth more than the change.

Two rules came out of the v0.11.0 work, both about the same class of failure —
**a check that is green because it never measured anything**. A `pytest` run
that never started, because its exit code came from `tail`. Two architecture
invariants that passed while checking almost nothing: one walked only `from .x`
imports and went blind when the bounded-context split moved most of them to
`..`, the other could `pytest.skip` itself when a module moved, so three
report-only guards switched off in silence and only the skip count (25 -> 28)
noticed.

Both rules were written into the vault, then into `AGENT_SHARED_PREAMBLE` §2 —
and that still left them where the failures did NOT happen: the preamble is
read by subagents, while both mistakes were made in the main thread.

`hooks_state.VERIFICATION_FRAGMENT` closes that: one fragment, stating one
class rather than two rules, injected into every SessionStart context.

- take the exit code from the COMMAND (`cmd > run.log 2>&1; echo "EXIT=$?"`),
  never from a pipe;
- prove a new invariant by PLANTING the violation it must catch, in the form
  the code actually uses, and watching the suite go red;
- a check that can skip itself when its target is missing must fail instead,
  and a rising `skipped` count after a refactor means checks turned themselves
  off.

`test_verification_fragment_reaches_every_session` asserts the two actions
rather than the heading — a fragment trimmed to a slogan would still pass a
title check — and it was verified the way the rule demands: the injection was
removed, the suite went red, the injection came back.

Full suite: 1 962 passed, 25 skipped.

---

# v0.11.0 — layers, and the three tails the audit left

Two years of features had settled into one flat namespace: 10 263 lines — 43%
of the engine — sat directly in `singularity/`, `cli.py` had grown to 1 511
lines holding every handler, and roughly 300 `print` calls lived inside domain
modules that a daemon or a hook could not reuse because of them. This release
draws the layers, and then closes what the 2026-07 audit had left open.

### Two nets before touching anything

- **ruff at zero**, not as a backlog: 226 findings auto-fixed, the rest fixed
  by hand or exempted in `pyproject.toml` with the reason. `test_lint_gate`
  keeps it there. What it found that 1 909 tests had not: a dead `import
  shutil` that three test files were patching THROUGH (the patch reached the
  global `shutil` that `cli_locator` actually calls), an unresolvable
  `-> NoReturn` annotation, two dead computations, four bound-and-forgotten
  names.
- **`test_architecture.py`**: only the presentation layer may print, exit the
  process or parse arguments; the domain may never import the CLI. Where that
  was false it was recorded as an EXEMPT list that may only SHRINK — and a
  stale entry fails the suite exactly like a new offender. Debt went 19 -> 7,
  with `sys.exit` and `argparse` both at zero.

### The layers

- **`config` raises instead of exiting.** `load_config` called `sys.exit(2)`,
  which is right for a CLI and unusable elsewhere — and the cost was already
  paid: `search/daemon.py` carried its own copy of the config validation
  because "it exits the process, which is fatal for a daemon". Now
  `ConfigError`, with the CLI printing the same lines and returning the same 2.
- **Notices left the domain.** `search.client`, `search.daemon` and `watch`
  stopped printing entirely, `llm` shed 27 of 47. A notice belongs to whoever
  owns the process (the daemon writes to its log file, a CLI run to stderr, so
  `mb search --json | jq` still receives only the payload). `_LiveStreamHandler`
  resolves the stream at emit time — that one property let 44 existing tests
  keep asserting on user-facing output unchanged.
- **Reports became data.** `status`, `stats`, `curator` and `gaps` each had a
  `run(cfg, as_json)` writing the same facts twice; the collectors now return
  and `render.py` formats. JSON output is byte-identical — it always was
  "whatever the collector returned".
- **`cli.py`: 1 511 -> 523 lines**, handlers grouped in `commands/` by subject.
- **The flat root became bounded contexts**: `knowledge/`, `machine/`,
  `telemetry/` (22 modules, root 31 -> 10 files). `config`, `doctor`, `llm` and
  `search/` deliberately stayed — the tests patch them heavily, and moving a
  monkeypatched module buys failures that appear only under a specific test
  order. No compatibility shims: one path to each module.
- **The vault layout is known in one place.** Thirteen call sites built paths
  by hand; `cfg.vault / rel` existed in three modules, one normalising Windows
  separators and two not, so `howto\x.md` resolved to a file that does not
  exist on macOS.
- **`contracts.py`** declares the four shapes that cross boundaries as
  TypedDicts (still plain dicts at runtime, so nothing serialised changes), and
  `test_contracts` compares them to what the code emits in both directions. It
  immediately found that one `Verdict` described two different shapes.
- **Nine import cycles removed** — all one mistake: `scout`/`imgqa` kept
  `SCHEMA_VERSION` in `cli.py` while `cli` imports `_contract`. And the engine
  does not import the media factory even once, so that ~8 000 lines can be
  separated whenever the owner decides; a test now fails if that reverses.

### The audit tails (AUD-17, AUD-18, AUD-21)

- **Hybrid retrieval.** Embeddings answer "what is this about" and miss
  `IndexLockedError` or `--per-signal`. BM25 + Reciprocal Rank Fusion (never a
  weighted sum: cosine and BM25 are incomparable scales), built on first use,
  off unless asked (`--hybrid` / `hybrid_search`). The evaluation set AUD-17
  asked for found two integration defects reasoning had missed: the candidate
  loop's `break` at the first sub-`min_score` cosine discarded exactly the
  exact-token hits, and the final sort by cosine undid the fusion.
- **Daemon counters.** `mb status` said the daemon was up and nothing about
  whether it answered well. Searches, errors, reloads, p50/p95 — counts and
  durations only, memory-only, and the tail rather than an average.
- **The inbox.** Capture now, classify later, file only on approval; `suggest`
  proposes and writes nothing. Self-review then caught the defect that would
  have made it worthless: captures were being indexed and reported by the
  curator, so drafts ranked against knowledge and `suggest` would have offered
  a capture as a duplicate of itself.

### Also worth knowing

Two silent failures surfaced during the move, both of the same family — code
that assumed its own location. `reflect` started looking for
`singularity/install/skill_manifest.py`; worse, `test_wiki_loop_report_only`
SKIPS a module it cannot find, so three report-only guards switched themselves
off and the suite stayed green while the invariant stopped being checked. The
skip count is what caught it (25 -> 28).

Full suite: 1 962 passed, 25 skipped.

---

# v0.10.0 — ten commands on the window, and the rest behind them

`mb` had grown to 23 top-level commands and 60 leaves — an honest reflection of
what the engine does and a bad first screen: the four commands used daily sat in
the same flat list as `uninstall` and `reflect`. This release narrows what the
CLI *shows* without moving anything.

### The window

- **`mb --help` lists ten**: search · learn · note · ingest · status · doctor ·
  curator · llm · service · setup. `mb help --all` prints every command, grouped,
  with each direct path spelled out. A bare `mb` answers with one line of state
  instead of an argparse error.
- **Groups rewrite argv; they do not wrap.** `mb service daemon start` becomes
  `mb daemon start` before the parser sees it, so a grouped path and its direct
  path are literally the same code — they cannot drift, and nothing that already
  calls the direct paths needed touching: 1878 existing tests, 33 skills and 3
  hooks are unmodified.
- **Help is generated from the parser.** One-line descriptions come from the
  `help=` text already declared in `_build_parser()`; `facade.py` owns placement,
  not prose, so a renamed command cannot leave a stale line in the window. Two
  bad descriptions were fixed at the source instead (`note` read "notes";
  `ingest` was two concatenated clauses).
- **The seven sibling CLIs are named** in the help footer (imgqa, design-scout,
  pattern-scout, component-intake, reel, golden, factory). Owner decision: they
  are a different subject area, so they keep their own entry points rather than
  becoming `mb` subcommands — but the help stops pretending they do not exist.

### A group is not a host

The first cut named the knowledge-care group `curate` — while the command
`curator` already existed and was written in 16 skill files, the SessionStart
hint and the vault's own notes. Functionally fine (direct paths kept working),
tests green, and the defect surfaced only through the owner's question "did you
update the skills for the new contract?". The choice was then between editing 17
places for a nicer verb or letting two names for one thing live side by side.

The fix removed the second name and split a concept that had been conflated:

- a **group** (`service`, `setup`) is a name that is *not* a command and exists
  only to front others;
- a **host** (`curator`, `status`) is a real command that *additionally* answers
  for a sibling: `mb curator nightly` and `mb status stats` now work, while
  `mb curator` still prints the report it always did.

`test_cli_facade.py` pins both directions — group names must not be commands,
host names must be — plus the invariant that keeps the window honest as the
engine grows: every parser command is either on the window or inside a group or
a host, and every name the facade lists exists in the parser. Rule recorded in
the vault: *Второе имя для существующей вещи оплачивается всеми её потребителями*.

### The measurement loop closes

A coverage check ("which command has a skill that leads to it?") found that
`mb reflect`, `mb stats` and the nightly trend line were mentioned by **none** of
the 33 skills — including `memory-curator`, whose whole subject is "how is the
memory doing". T14 shipped those measurements to "decide on further complexity
from data rather than impressions"; the data had been accumulating unread.
`memory-curator` gains a *Здоровье по данным* section: thresholds tied to the one
declared goal (G1, ≥80% of session knowledge lands in the vault) because the
engine deliberately has none, the denominator checked first (percentages over 3
sessions mean nothing), and every signal mapped to one action. SessionStart now
also points at the window, so an agent looks the surface up instead of guessing.

### Upgrading

Nothing to migrate — no data, config, index or command name changed;
`docs/UPGRADE.md` has the section. One behavioural change: a bare `mb` and
`mb help` exit **0** instead of **2**, because neither is an error any more.
Everything else, including `mb <unknown>` → 2, is untouched.

Full suite: 1909 passed, 25 skipped.

---

# v0.9.1 — the machine is asked, and D7 stops accusing the wrong install

Two follow-ups, both from running the release on a second machine.

- **`A8-machine-setup` and a prompt at the two moments it is actionable.**
  Making the config per machine turned "a value that is wrong here" into "a
  value that is absent here" — quieter, and nothing was asking for it. An
  arriving vault carries the settings and none of the local answers, so the
  migration and the installer now print what is unstated (`code_repos_dir`,
  `projects`, `llm`, `imgqa_ml_venv`) with the exact command for each. Every
  item is optional, which is why it is a prompt at install and migration rather
  than a standing warning; `A8` reports the same list and WARNs in one case
  only — a machine that has stated nothing beyond which vault to use.
- **`D7-cli-on-path` asked the wrong question.** It compared where the `mb`
  launcher FILE sits against `<engine>/.venv`, so an editable install into a
  system Python — script outside the engine, importing the engine — was
  reported as "another Singularity install is shadowing this one", with a fix
  that tells the user to remove a working install. It now resolves which engine
  the `mb` on PATH actually imports: same engine through another interpreter is
  a PASS, a genuinely different engine keeps the warning, and "could not be
  determined" says that instead of naming a cause.

---

# v0.9.0 — the config leaves the vault, and nothing in the engine names a drive

One owner decision drove all of it: **paths belong in a config created at
install, not in code** — and that config does not live in the vault, because a
vault travels between machines and every absolute path inside it is wrong on
arrival. v0.6.0 reported that problem (`A6-config-paths`, nine dead keys after
one clone). This removes the possibility.

### The config is per machine

- **`~/.singularity.json` is THE config.** Everything that was in
  `<vault>/.mb/config.json` now lives there: paths, behavioural settings, the
  project registry. `.mb/` keeps derived state and the one thing it holds that
  a rebuild cannot recreate, `sources/`.
- **It records decisions, not defaults.** `save()` writes only values that
  differ from `DEFAULT_CONFIG`; a real machine's file went from 42
  materialised keys to 9. Defaults stay in code, where they are values rather
  than machine facts, and the file stays small as the schema grows.
- **Migration is seamless and one-time.** The next `mb` command adopts a
  pre-0.9 vault config, writes the machine one, and renames the old file to
  `config.json.migrated-<ts>` — kept, not deleted. It refuses to run when the
  machine already has settings, so opening someone else's vault cannot
  overwrite this machine. A legacy file that does not parse is a loud refusal,
  never a silent fresh start: skipping it would drop every setting the user
  had.
- **Hooks keep working mid-upgrade.** They run before the first `mb` command
  performs the migration, so `read_config_tolerant()` and
  `config_unreadable()` fall back to the vault copy while the machine config
  is still empty — through one shared `active_config_path()`, because a hook
  that validates one file and reads another is worse than one that does
  neither.
- **`A7-config-location`** reports a config still sitting inside a vault.

### Nothing in the engine names a drive

- **`DEFAULT_VAULT_WINDOWS` is gone.** The built-in candidates are
  home-relative only. That constant was justified as "a last resort costs
  nothing" and cost twice: it outlived its own machine's move to another
  drive, and it shipped a dead drive letter to everyone else.
- **The installer ASKS.** `--vault` wins and creates the directory; otherwise
  a resolved path is shown for confirmation rather than assumed, and nothing
  configured means asking until there is a usable path. Non-interactive runs
  never prompt — CI and scheduled runs fail naming the flag instead of hanging
  on a question nobody can answer.
- **Agent and skill text carries `{{VAULT}}`**, substituted at install. The
  literal path used to be repeated in 18 files with a matching sentinel in
  `register_agents`, a pairing that silently stopped matching when the
  authoring machine changed drives. A token cannot drift.
- **The vault is named only where a path is actually needed.** For a mention
  that merely identifies it, `mb search` already knows where it is — naming
  nothing beats substituting something. Eleven decorative mentions removed, so
  only **4** skills need rendering instead of linking; the rest stay live
  links into the repo. Paths after the token are forward slashes now:
  `{{VAULT}}\rules\` would have rendered as `/Users/k/vault\rules\` on macOS.

### Test isolation

Two more guards, and both earned it immediately. `_no_real_machine_config`
redirects the machine config AND takes the real file's bytes as a witness:
redirecting the module attribute is not enough, because a module-scope
`from singularity.config import CONFIG_FILE` binds the original path at import
time — which overwrote this machine's real config before the witness existed.
The same guard then exposed a v0.8.0 test that only passed because it read the
developer's real `~/.singularity.json` and found a real vault.

Full suite: 1878 passed, 14 skipped.

### What the baseline caught that the release had missed

`docs/v0.9.0-install-doctor-baseline.md` — the snapshot came back red twice
before it came back honest, and every reason was real.

- **Four more drive letters were still compiled in.** A scan added to the
  baseline found `imgqa`'s ML venv (`D:\venvs\imgqa-ml`) and `mb llm setup`'s
  install directory (`E:/llama.cpp`). Both are gone: the venv must be named
  through `IMGQA_ML_VENV` or `imgqa_ml_venv`, and `mb llm setup` requires
  `--dir` the first time instead of downloading gigabytes onto a drive that may
  not exist. Neither now fails as "not found at <a path you never chose>",
  which reads as a broken install rather than an unconfigured one.
  `C:\Windows\Fonts` stays — that belongs to the OS, not to a machine.
- **A daemon started before the migration keeps reading the old file.** It
  holds the pre-0.9 module, so its config path still resolves into the vault
  and `mb daemon reload` answers "nothing changed" while the settings look
  applied everywhere except where they are read. Migration is the one moment
  that is known to have happened, so it now says `run mb daemon restart` when
  it sees a live daemon.
- **`MB_HOME` no longer isolates a test run**, because it moves the vault and
  leaves the command reading this machine's real config — which is how the
  baseline's own `config_write_path` section ended up grading the developer's
  machine. It fakes `USERPROFILE`/`HOME` now.

### Also in this release, marked as such

- **`reel` — EXPERIMENTAL, in development.** The video-editing CLI and its
  `reel-editor` skill land in master unfinished, by owner decision, carrying
  the label rather than waiting: the subcommand contract is still moving,
  backward compatibility is not promised, and it has no `mb doctor` check of
  its own. Shipping it silently as a finished capability was the alternative,
  and that is how a prototype becomes a promise.

---

# v0.8.0 — a rule that is not there when the decision is made is not a rule

Rollup of everything shipped on 2026-08-04 as v0.6.1 → v0.7.1: 21 commits,
27 files, 1754 → 1806 tests. The per-release sections below stay as they are;
this one is the shape they turned out to share.

Every defect in this batch had the same anatomy. The knowledge existed. The
rule was written, the check was implemented, the diagnostic printed the right
words — and none of it was present at the moment someone chose what to do. Six
times in one day, in six different places:

| Release | The knowledge that existed | Where it wasn't |
|---|---|---|
| v0.6.1 | `doctor A6` names the broken key exactly | `config set` could not resolve that key's notation, and four separate read-backs confirmed the write anyway |
| v0.6.2 | the sanitizer knows the filename changed | nobody was told, until the link broke weeks later in `curator gaps` |
| v0.6.3 | the vault guard was written and registered | it was switched off |
| v0.6.4 | the guard says "use `mb note new`" | two guarded roots had no such type — and the skill authorized the bypass in writing |
| v0.7.0 | `A6` reports a dead model path | the registry was read-only, so there was nothing to do about it |
| v0.7.1 | "artifacts live in the vault" was an active rule | it was absent where the write path gets chosen |

The fix was the same shape too: put the statement at the point of decision, from
one source, and add a test that fails when any surface drops it. Three shared
fragments now work that way — `CAPTURE_HOOKS_FRAGMENT`,
`VAULT_WRITE_POLICY_FRAGMENT`, and `DEFAULT_CONFIG` as the single home of the
guard's default — because a second hand-maintained copy is how the guard's own
default nearly survived being flipped.

### What changed for someone using this

- **`mb config set`** resolves dotted keys (`projects.x.path`,
  `llm.models.q.args[14]`), refuses unknown keys with a "did you mean", and
  never invents structure. `--allow-unknown` for the deliberate case.
- **`mb llm add` / `mb llm remove`** — the model registry can be written to.
- **`mb note new`** covers every guarded root, takes `--filename`, and names the
  link target when the filename differs from the title.
- **`mb project add`** merges instead of rebuilding an entry.
- **The vault guard is on** in `audit` for new installs; existing configs keep
  their explicit value.
- **All four clients** (Claude, Codex, Gemini, Grok) carry the vault-only write
  policy, and `mb-memory` gates both *where* and *with what* before a write.
- **`pip install -e ".[dev]"`** exists; a fresh clone can run the suite.

### Known state, not swept under

`A6-config-paths` still WARNs on this machine: 8 paths belong to the Windows box
this vault came from (three projects, `llama.cpp`, three `.gguf`). That is the
check doing its job — the values cannot be guessed, and the machine simply does
not have those files. Everything else in `mb doctor` is green.

---

# v0.7.1 — the vault-only write path, present where the path is chosen

Closes **VSO-01**, open since 2026-07-13.

The incident it came from ([[Error - Vault staging duplicates]]): a client
edited 14 vault documents as copies under the user's profile root and copied
them back, because the sandbox exposed that directory as writable and a mirror
looked like the path of least resistance. No unique data was lost — every
staging file turned out byte-identical to, or contained in, its canonical
version — but for a while the machine had two working sets of the same notes,
and only a hash audit could say which was authoritative. Direct patching of the
vault path was verified to work during cleanup; the workaround had never been
necessary.

A rule saying "artifacts live in the vault" already existed and was active. It
just was not present at the moment the write path gets chosen, which is the
only moment it could have helped — the same shape as VSO-02, where the skill
authorized the very bypass the guard was flagging.

`VAULT_WRITE_POLICY_FRAGMENT` joins `CAPTURE_HOOKS_FRAGMENT` in
`singularity/hooks_state.py` and is injected verbatim into all four surfaces
that decide a write path: the Claude SessionStart payload and the managed
blocks of `~/.codex/AGENTS.md`, `~/.gemini/GEMINI.md`, `~/.grok/AGENTS.md`. One
source, because four hand-maintained copies of a policy drift — as the guard's
own default did a release ago.

It says four things: write **directly** to the vault (or via `mb note new` /
`mb learn`); never create an editable mirror and copy it back; external folders
are for input and for an explicitly requested export; and if a direct write is
genuinely blocked, **stop and say so** rather than routing around it. Reading
vault files and copying *into* the vault stay explicitly fine — the rule is
about editable duplicates, not about the outside world.

`mb-memory/SKILL.md` gains the matching destination gate above the "чем писать"
gate added in v0.6.4: together they answer *where* and *with what* before the
first write.

`tests/test_vault_write_policy.py` asserts the policy on every surface and that
each imports the shared constant instead of paraphrasing it. Verified by
deleting it from one registrar: the suite fails.

No home-directory scanner was added — that was ruled out in the task's own
acceptance as a separate privacy decision.

Full suite: 1806 passed, 25 skipped.

---

# v0.7.0 — the registry gets a write half, and a typo stops passing for a fix

Three CLI frictions, each filed after `mb doctor` or the vault guard pointed at
a command that could not do the job. Two of them were the owner's call to make
and were made explicitly.

### `mb llm add` / `mb llm remove`

The model registry was read-only. `mb llm list` on an empty one said *"add to
llm.models in .mb/config.json"* — by hand, in the file `mb config set` owns,
which is exactly the edit the vault guard flags. And after a move between
machines, `A6-config-paths` reports dead `llm.models.*.gguf` paths with nothing
to offer: `config set` can repoint one, but a model that is gone for good has to
be *removed*, and no command did that. `mb project` has had `add`/`remove` since
T03; models had neither.

```bash
mb llm add q9b /models/q9b.gguf -- -ngl 99 --jinja
mb llm remove q9b
```

`<name> <gguf>` positionally, mirroring `mb project add <id> <path>`. Adding an
existing name repoints it and **keeps its `args`** — the same silent-loss bug
`project add` had in v0.6.1. A path that does not exist yet is registered with a
note, not refused: configs get prepared before weights are downloaded, and A6
already reports paths that resolve nowhere.

`remove` refuses while that model is the one being served, unless `--force`.
Stopping someone's running server as a side effect of a registry edit is not a
config change, it is an outage. With `--force` the entry goes and the server is
left alone, with a line saying so.

`A6`'s fix line now names both registries instead of sending everyone through
`config set`.

### `mb config set` refuses an unknown key

`mb config set modle 60` wrote the key and warned (v0.6.1+). Nothing reads it,
so the write is a no-op the user reads as a fix — and `modle` is one keystroke
from `model`. **Owner decision:** refuse by default, with the near-miss named.

```
$ mb config set modle 60
[mb] modle: unknown config key (did you mean: model?)
      nothing reads it — write it anyway with --allow-unknown
$ echo $?
1
```

The legitimate case — a connector key, or one a newer build reads — stays one
explicit flag away.

### `mb note new --filename`

The vault's convention keeps the type in the filename and `title:` clean
(`rules/Rule - X.md` holding `title: X` — 28 of 38 rules look like that). A
single positional argument cannot say both, so the prefix ended up inside the
title, where it does not belong. **Owner decision:** an explicit flag, default
behaviour untouched.

The v0.6.2 warning now fires only when the sanitizer changed what was actually
asked for — with `--filename` a different filename is intent, not a surprise. A
stem that strips to nothing is refused rather than creating a stemless `.md`.

Full suite: 1797 passed, 25 skipped.

---

# v0.6.4 — the guard's advice is now takeable everywhere it applies

v0.6.3 turned the guard on. This closes the two things that made ignoring it
reasonable.

### Every guarded root has a `mb note new` type

`vault_guard` watches ten managed roots and tells you to use `mb note new` /
`mb learn` instead. Two of them had no type behind that advice: `_moc/` and
`changelog/`. Creating a dashboard, or the first note of a new month
(`changelog/YYYY-MM.md` does not exist yet on the 1st), left exactly one route
open — the one the guard warns about. A warning against the only available
action is noise, and noise is how a guard gets trained away.

`note new` gains `moc` and `changelog`. Neither has a template of its own and
both fall through to the generic body; the point is that the sanctioned path
exists. `learnings/archive` stays deliberately uncovered — notes arrive there
by being moved, never by being created.

`tests/test_note_covers_managed_roots.py` holds the two lists to each other in
both directions, so a future guarded root cannot ship without a way to write to
it, and a type cannot point outside the guarded set. Verified by reverting the
change: three tests fail.

### The skill said the opposite of the guard

`mb-memory/SKILL.md` is what the assistant actually reads, and it said *"крупные
записи — редактируй `.md` напрямую (Edit/Write)"*. So the bypass was not a
lapse — it was the documented instruction, and the guard was arguing with the
skill. The skill now carries the gate itself: a NEW file in a managed root goes
through `mb note new` regardless of size (create with the command, fill in with
Edit), editing an existing note stays Edit/Write, config goes through
`mb config set`, and searching goes through `mb search` / `mb curator gaps`
rather than `grep`.

It also opened with one machine's Windows vault path, wrong on every POSIX
host — the same class as #14, in the first file the assistant reads. Now it
points at `mb doctor`'s `A2-vault-obsidian` instead, and a test fails on any
drive-letter path in the file.

Full suite: 1777 passed, 25 skipped.

---

# v0.6.3 — the vault guard is on by default

`vault_guard_enabled` ships `true` in `audit` mode (owner decision, 2026-08-04).

The check was written, registered as a PreToolUse hook, and switched off — so
the thing it exists to catch went uncaught. It caught it the same day it was
turned on: two rule notes created with a client's `Write` tool instead of
`mb note new` skipped `templates/rule.md`, and the miss was invisible because
skipping a template does not fail — the `source:` field and the required
sections are simply absent, and the note reads fine. A human noticed, days
later, by accident.

`audit` only annotates the write (`[vault-guard audit] New file created
directly in managed knowledge root 'rules/', bypassing the sanctioned CLI —
prefer mb note new / mb learn`). It never blocks: `deny` stays explicit opt-in,
per the 2026-07-12 Open Decision.

### Nobody is flipped by an upgrade

`Config.save()` materializes every default into `config.json`, so any install
that has ever saved its config already carries an explicit value and keeps it.
The new default reaches new installs. `docs/UPGRADE.md` now states the general
rule rather than repeating "every opt-in defaults to off", which this release
would have made false.

### Two defaults were one too many

The hook reads a raw config dict (`read_config_tolerant` applies no defaults),
so it carried its own literal `False` — a second copy that would have quietly
outlived this change and left the guard off exactly where it matters. It now
falls back to `DEFAULT_CONFIG`, as does `mb doctor`, and a test pins the two
together by monkeypatching the default and asserting the hook follows.

`D5-vault-guard` no longer calls `disabled` the default: it prints
`disabled (vault_guard_enabled: false)`, because "off" is now somebody's
decision rather than the shipped state.

### One case where the default must not speak

An unparseable `config.json` used to mean "guard off" for free — `{}` and the
literal `False` agreed. With the default flipped, that same silence would have
meant "guard on", annotating writes on a config the user could not have
expressed anything in. New `config.config_unreadable()` separates "key absent
in a readable config" (→ default applies) from "file exists but does not
parse" (→ the hook stays silent; `mb doctor` is where a broken config gets
reported).

Full suite: 1771 passed, 25 skipped.

---

# v0.6.2 — a silent rename, and a suite a fresh clone could not run

Two small frictions, both hit during the v0.6.1 release itself.

### `mb note new` says when the filename is not the title

The filename is the wikilink target, and the sanitizer — not the author —
decides it: everything outside letters, digits, space, `-` and `_` becomes `_`
(`cli.py`). A comma in a title is enough, so `[[<title>]]` written from memory
in another note points at nothing. Nothing said so at creation; the break
surfaces much later in `mb curator gaps` as a missing page, sitting among
genuinely unwritten notes, with no hint that a rename caused it.

`mb note new` now prints the working link target when it differs from the
title, at the one moment the difference is still cheap to fix. The `title:`
frontmatter stays exactly as typed — only the path is sanitized, and that
asymmetry is the thing worth naming out loud.

### `pip install -e ".[dev]"`

The installer puts the runtime into `.venv` and stops there, which is right for
a user and wrong for anyone who then tries to run the suite: `python -m pytest`
answered `No module named pytest`, and the only way to learn about
`requirements-test.txt` was to read the CI workflow.

The new `dev` extra carries no second copy of any pin — it self-references
`singularity[imgqa,secrets]` and adds `pytest`, so the base and extra pins stay
declared exactly once. `requirements-test.txt` remains what CI installs;
`tests/test_dev_extras_parity.py` pins the two to the same package set, because
a pin added to one and forgotten in the other yields a green build that fails
on the next person's machine. Verified by breaking it: adding a package to
`requirements-test.txt` alone fails the test.

Full suite: 1769 passed, 25 skipped.

---

# v0.6.1 — the fix `mb doctor` prints is one you can actually type

Found while acting on v0.6.0's own output on the machine it was released from.
`A6-config-paths` reported eight paths belonging to another operating system
and printed the fix: `mb config set <key> "<path>"`. Typing it changed nothing,
and said otherwise four times.

### `mb config set` resolves the notation the doctor speaks

`config set` did `{**cfg.data, args.key: value}`, and `Config.get` matched flat
keys only. Every key `A6` names is nested — `projects.<id>.path`,
`llm.server_bin`, `llm.models.<name>.gguf`, and the `args[14]` index form — so
the printed fix wrote a literal top-level key `"projects.singularity.path"`
that no reader ever looks at, and the real value stayed where it was.

What made it expensive is that nothing disagreed:

- the command echoed `projects.singularity.path = "/Users/…"`;
- `mb config get <same key>` read the value back — from the junk key, by exact
  match, which is the read-back you would reach for to confirm the write;
- `mb daemon reload` answered `config_changed: ["projects.singularity.path"]`;
- `mb config validate` stayed quiet, because unknown keys are warnings and
  `set` never printed them.

Then the next `mb doctor` repeated the same WARN, verbatim.

- **`parse_config_key` / `get_config_key` / `set_config_key`** (`config.py`)
  speak exactly what `config_paths.path_entries` emits, index form included.
  The notation was never chosen here — it is the one the diagnostic already
  prints, so the writer has to speak it or the printed fix is a dead end.
- **Intermediate nodes are never invented.** An unknown section or leaf is an
  error that names what *is* there (`projects.singularity has no key colour
  (present: about, group, path)`) and points at `mb project add` for projects.
  Creating structure stays with the command that knows its shape; guessing it
  is how the junk key got written in the first place.
- **`Config.get` answers for dotted keys**, so `mb config get` and the daemon's
  `config` read-back cover the same keys `set` writes — one notation across the
  write, the read, and the diagnostic.
- **`config set` surfaces the unknown-key warning** instead of writing a typo
  in silence. It still writes: unknown-to-the-schema is not the same as wrong
  (connectors and forward-compatible settings live there).
- **`mb project add` merges into an existing entry.** It is the supported way
  to repoint a project after a move between machines, and it was rebuilding the
  entry from the flags alone — dropping `group` and `about` without a word,
  a loss that surfaces much later as a project missing from its group.
- **`A6`'s fix line quotes a key that is actually broken**, and names
  `mb project add` when projects are among them.

### The baseline went around the command under test

v0.6.0 shipped a live `set → read-back → reload → restore` cycle against a
running daemon (#12) and it passed honestly — but it wrote through `cfg.data`
directly, on the flat key `excerpt_chars`. It never typed `mb config set`, and
never touched a nested key, so it could not see that the write path resolved no
dotted key at all while `A6` printed nothing but dotted keys.

`tools/install_doctor_baseline.py` gains `config_write_path`: five cases driven
through the real CLI against a throwaway vault — dotted write, indexed write,
unknown section, dotted read-back, and `project add` metadata retention —
graded on the config as it lands on disk. Run against the pre-fix tree, four of
the five fail; the one that passes is the read-back, still confirming the junk
key, which is the whole reason this was believable.

While snapping the v0.6.1 baseline, the same tool graded its own version-
manager matrix 3/5 — not a regression, a measurement reading the operator's
terminal. `cli_locator` consults `$NVM_BIN` / `$NVM_DIR` / `$FNM_MULTISHELL_PATH`
before falling back to `~`, so faking `Path.home()` does not redirect it, and
the synthetic layouts were invisible whenever the snapping shell had nvm
sourced. The suite has neutralized that surface since #10
(`_no_real_version_managers`); `locator_matrix` now suppresses the same
variables for the duration of the run and records which it suppressed. The
search-location count is back to 8 on this host, matching v0.6.0.

21 tests in `tests/test_config_nested_keys.py`. Full suite: 1762 passed,
25 skipped.

---

# v0.6.0 — the install is honest about itself (field issues #9–#16)

Eight issues filed from one fresh macOS install of v0.5.1, all of the same
family: **the installer exited 0 and every diagnostic was green while a feature
was quietly unusable.** Nothing here is a new capability — it is the install
and the doctor learning to tell the truth about what actually works on the
machine in front of them.

### The install could not be run, and then could not be called

- **`install/install.sh` is executable again (#9).** It was stored in the index
  as `100644`, so the exact first command the README documents failed with
  `permission denied` (exit 126) on every fresh POSIX clone. `tests/
  test_repo_file_modes.py` pins the index mode — `.gitattributes` cannot set
  the exec bit, only the index can, and it is easy to drop again. README
  documents `sh install/install.sh` as a fallback.
- **The CLIs are put on `PATH` (#15).** `pip install -e` generates the seven
  console scripts into `<engine>/.venv/bin`, which nothing adds to `PATH`, so
  every documented `mb …` command failed with `command not found` on a machine
  the installer had just called healthy — and the `bin/` shims, the apparent
  alternative, shipped `100644` too. New phase `4e` symlinks the console
  scripts into `~/.local/bin` on POSIX (printing the exact `export PATH=…` line
  for the user's own shell rather than editing their rc file) and appends the
  venv `Scripts` directory to the Windows **user** `PATH` via the registry —
  not `setx`, which truncates at 1024 characters and flattens `REG_EXPAND_SZ`.
  Ownership follows the rule skills and hooks already use: only links resolving
  back into THIS engine are created, and `mb uninstall` removes exactly those.
  The tool list is parsed from `[project.scripts]`, so it cannot drift.
- **`D7-cli-on-path`** answers what `D4-cli` never asked about our own CLI: is
  `mb` callable, and is it *our* `mb`. Two installs answering one vault is
  quieter and worse than none.

### Launching stopped depending on where you were standing

- **Nothing launches via `python -m` any more (#16).** `-m` puts the current
  working directory first on `sys.path`; a directory containing a folder named
  `singularity` — the parent of a standard clone, and `$HOME` itself when you
  follow the README's `git clone <url> ~/singularity` — is then adopted as a
  namespace package and shadows the installed one, killing the CLI with
  `ImportError: cannot import name '__version__' from 'singularity' (unknown
  location)`. An empty decoy directory is enough to reproduce it. The `bin/`
  shims now exec the generated console script (immune, and kept in sync by
  pip), falling back to `bin/_launch.py` — running a *file* puts that file's
  own directory on `sys.path[0]`. The same defect was live in three more
  places the report did not cover, all of which inherit a working directory
  nobody chose: the detached daemon spawn, and the scheduler jobs in all four
  backends (Windows `<WorkingDirectory>`, launchd, systemd, cron `cd … &&`) —
  on the README's own Linux layout every nightly rebuild and login watcher was
  dying silently. `D6-posix-shim` now runs the shim from two directories it
  creates itself, one of them shadowing, instead of inheriting doctor's.
- **`bin/*` are `100755`**, so the shim route works on a fresh clone too, and
  the `.cmd` shims no longer carry an absolute engine path that only ever
  worked on the author's machine.

### The daemon stopped serving a config nobody could change

- **`mb daemon reload` reloads the config, not only the indexes (#12).** It
  reloaded indexes off the `Config` captured at daemon start, so `mb config
  set` wrote a value the serving process never saw — `mb index rebuild --code`
  reported `{"files": 0}` indefinitely while an in-process rebuild of the same
  config indexed 205 files, and the command whose name promises exactly this
  answered `{"status": "reloaded"}` while changing nothing. Reload now re-reads,
  validates and migrates the file and reports which **keys** changed (names
  only — the response crosses a socket). A changed `model` is reported as
  `restart_required`: the encoder cannot be swapped under a live engine.
- **`mb config set` says so when a running daemon still holds the old value**,
  via a new read-only single-key read-back. A daemon that predates the command
  is treated as "did not answer", never as a mismatch.
- **`mb daemon restart` exists.** The remedy for anything `reload` cannot
  rebind had no single command, and `reload` — the one that sounds like it —
  was the one that did not do it.
- **A configured-but-unreachable corpus root is a refusal, not `files: 0`.**
  Corpora carry `root_must_exist`, set only for the code corpus: the user typed
  that path. The vault and `.mb/sources/` keep the permissive default, so
  `mb ingest index` on a never-ingested vault still works.

### Checks that confirmed existence now confirm function

- **`K1-code-index` treats "built from 0 files" as a WARN (#13).** It reported
  `PASS: 0 chunk(s) from 0 file(s)` — a status contradicting its own message —
  and would otherwise have caught the daemon-config bug immediately. Signal
  quality had been *inverted*: pointing `code_repos_dir` at a real path turned
  a true warning into a false pass while the feature stayed equally broken.
  `mb status` no longer prints the empty count in a healthy tone.
- **`D8-git-identity` checks that git can commit, not that git exists (#11).**
  A new machine has no `user.name`/`user.email`, the vault is a git repo for
  most users, and several skills here run `git commit` inside it — so an
  install passing every check was one step from `Author identity unknown`.
  Read-only, `SKIP` when the vault is not a work tree, honours a per-repo
  identity, and **never** auto-fixed even with `--fix`: a commit identity is a
  personal choice permanently embedded in published history.
- **`A6-config-paths` validates every path in the config (#14).** The vault is
  built to travel and `.mb/config.json` travels with it, but only
  `code_repos_dir` was ever checked — `llm.server_bin`, every `gguf`, the
  `--chat-template-file` inside `llm.models.*.args` and every
  `projects[].path` arrived dead on a new machine and went unmentioned. A value
  matching `C:\…` on a POSIX host is not missing, it is from another operating
  system, and now gets one statement naming every affected key instead of nine
  confusing absences. Nothing is rewritten automatically: the correct values
  cannot be guessed.

### A CLI installed under a version manager is found, not blamed

- **#10.** With `@google/gemini-cli@0.53.1` genuinely installed under nvm, the
  SessionStart and AfterAgent hooks were silently skipped and the message
  named a constraint the machine already satisfied (`requires gemini >=
  0.26.0`). nvm is a shell function sourced from an interactive rc file, so the
  CLI is invisible to any non-interactive process — an installer, a CI job, the
  scheduled jobs this project registers, anything under `sudo`. The new shared
  `install/cli_locator.py` probes `$NVM_BIN`, the nvm versions tree (newest
  Node first, ordered numerically), fnm, volta, asdf, bun, `%APPDATA%\npm` and
  the homebrew/local prefixes. "Installed but unreachable" is now its own
  reported state with an actionable hint, distinct from "too old", and
  `doctor --fix` can finally repair it — the old gate was `supported`, false
  whenever the version was unknown, so the hook branch was unreachable in
  exactly the state that needed it.
- **`mb doctor`'s summary repeats WARN lines, not only FAIL.** A phase that
  skipped a requested feature must not look identical to one that succeeded
  when the exit code is 0.

### Test isolation

Two new machine surfaces meant two new autouse guards in `tests/conftest.py`,
in the spirit of the existing `_no_real_scheduler` — and both earned their
keep immediately. `_no_real_user_path` caught a test that was about to delete
`E:\Singularity\.venv\Scripts` from the developer's real user `PATH`;
`_no_real_version_managers` stops a test that says "this CLI is not installed"
from finding the developer's own nvm copy.

Verified on a real machine as well as in tests: `PASS 33 | WARN 2`, both WARNs
being genuine findings the previous doctor never surfaced. Full suite: 1754
passed, 12 skipped.

---

# v0.5.1 — hotfix: a dying daemon no longer deletes its successor's state

Reported from the field as issue #8 (reproduced twice on macOS during normal
restarts), present in every version since daemon identity state was introduced
in AUD-11.

- **The exiting daemon's cleanup is now conditional on still owning the state.**
  `stop_daemon()` returns ~0.5s after SIGTERM, but the daemon only polls its
  shutdown flag *between* requests — mid-request it outlives its own stop by as
  long as that request runs, and since 0.4.0 in-daemon index writes
  (`{"cmd":"index"}`) made multi-minute requests the normal case. The starter
  meanwhile spawned a replacement that bound a port and wrote **its**
  `daemon.json`/`daemon.port`/`daemon.pid`. The old daemon's `finally` block
  then unlinked all three unconditionally — deleting the *successor's*
  identity. The starter's ping loop found no port, timed out after 30s and
  reported `FAILED to start` for a daemon that was up, and every later
  `ping`/`search` failed until the orphan was killed by hand. This reliably hit
  installer phase 3b, `mb model install`'s restart and `mb doctor --fix
  --heavy`. `SearchDaemon._cleanup_state()` now reads the state file first and
  removes nothing if a newer instance owns it.
- **Ownership is decided by `instance_id`, not pid.** The reporter's patch
  compared pids; `instance_id` is a fresh uuid4 per daemon and cannot be reused,
  while a pid can be — and pid reuse is the same class of race this fix exists
  to close. Legacy state files written before AUD-11 carry no `instance_id`, so
  the pid comparison remains as the fallback. Unreadable or absent state means
  nobody claims ownership and the instance's own leftovers are still removed.
- Five regression tests pin it: the successor survives both a differing pid and
  a reused pid, an instance still cleans its own state, legacy pid-only state
  works, and missing/corrupt state does not strand port/pid files.

Verified on a real machine against the live daemon: a simulated predecessor
cleanup left the running daemon's identity intact and it still answered `ping`.

---

# v0.5.0 — CODE-INDEX release hardening (C10)

Everything here came out of two adversarial reviews — one of a proposed design
before it was written, one of the branch's release readiness — plus the first
cross-platform run this work ever had.

- **Scoped the incomplete-scan refusal to subtrees that can actually hold
  content.** C09's guard recorded EVERY `os.walk` error, but the vault corpus
  walks into `.git/`, `.obsidian/` and `.mb/` (they are filtered per file by
  `should_skip`, not pruned). One unreadable directory inside `.git` therefore
  froze the vault index permanently — `mb index update` AND `mb index rebuild`,
  i.e. including the documented escape hatch — over a subtree guaranteed to
  contribute zero chunks. Errors under pruned/skipped paths are now ignored;
  an unreadable INDEXABLE directory still refuses, pinned by its own test.
- **Directory links are followed by the code corpus, with cycle protection.**
  Verified with both link kinds in one `os.walk`: a Windows junction is
  traversed (Python reports `islink=False`), a POSIX symlink is not. The
  documented way to index a repository living outside the root therefore worked
  on Windows and silently indexed nothing on macOS/Linux. Every visited
  directory is now keyed by realpath, closing a loop that was already reachable
  on Windows through a junction with no opt-in at all. The vault and sources
  corpora deliberately do NOT follow links out of themselves.
- **Machine-specific literals became defaults.** `resolve_vault`'s built-in
  fallback is a candidate list (`~/memory brain`, `~/Documents/memory brain`,
  then the historic path), replaceable with `MB_DEFAULT_VAULT`; the vault path
  inside `singularity/agents/*.md` is a template default that
  `install/register_agents.py` substitutes only when this machine's vault
  differs, so the symlink fast path — edit the repo, every agent updates — is
  preserved where it applies.
- **`mb selftest` no longer fails on a healthy system.** It searched for a
  random token and required it in the top-5 of the whole vault: a SEMANTIC
  search for a string with no semantics, which degraded as the vault grew.
  Confirmed by attribution — failing at 3244 chunks on the previous commit
  after passing at 3216. The probe now carries the note's own wording and is
  scoped to the throwaway folder. This was a live v0.4.0-era defect.
- **CI ran on a release branch for the first time.** The push trigger listed
  only `master` and `feat/**`, so the one kind of branch whose purpose is
  pre-tag verification was never covered; `release/**` and `fix/**` added.
- Uniform unavailability handling in `mb nightly`: the memory and sources steps
  now report `skipped` for an unreachable root like the code step already did.
- Hardening: the vault-candidate probe tolerates `OSError` (macOS TCC raises
  EPERM for `~/Documents` until granted, which would have surfaced as a
  traceback instead of the actionable "no vault configured" message).

**Cross-platform evidence.** GitHub Actions could not run (account billing), so
the suite was executed on real Linux in Docker: **1630 passed, 28 skipped** on
Python 3.10.20 and 3.12.13, on a case-sensitive filesystem. The link-traversal
tests ran rather than skipped — same 34 in that file as on Windows — so POSIX
symlink behaviour is verified by execution, not inferred. macOS remains
unverified.

**Decision D2 re-measured and CONFIRMED, not relaxed.** With the corpus
recomposed (vendored SDK out, this project's own 204 files in), `--with-code`
took 18/50 slots, displaced 13/50 notes and changed top-1 on 4/10 queries —
against 10/50, 4/50 and 0/10 before. The earlier permission to merge code into
the default result set was a property of that corpus, not of the design: it
collapsed on the addition of a single first-party repository. Code stays
opt-in.

Tests **1636 → 1659**.

---

# v0.5.0 — CODE-INDEX hardening: scan guards + vendored exclusion (C09)

Follow-up to C00–C07, driven by an adversarial design review of a proposed
multi-root `code_repos_dir`. **The multi-root change was dropped** — the review
showed it silently breaks `SKIP_REPOS` and `*/migrations/*` (both match the
FIRST segment of the un-prefixed sub-path, which a per-repo root moves into the
prefix), needs a schema migration to avoid re-keying a live corpus, and cannot
be expressed through `mb config set` on Windows at all (`json.loads` rejects a
backslash path inside a JSON list and silently stores the bracket text). A
directory junction reaches the same goal in one command with no code.

Two of that review's blockers turned out to be **live bugs in the shipped
single-root code**, and those are fixed here:

- **An unavailable corpus root silently purged the index.** `_scan_corpus`
  returns `{}` for a missing root, `_update` classifies every known file as
  deleted and the keep-mask drops their chunks; `_rebuild` committed the empty
  generation outright. An unmounted drive or a synced folder mid-reconnect
  destroyed a corpus that costs minutes to rebuild. Now `CorpusRootUnavailable`
  refuses and leaves the index untouched — but only when there is something to
  lose, so bootstrapping an empty corpus over a missing root still works.
- **`os.walk` had no `onerror`,** so a denied ACL or a locked subtree yielded
  nothing and took the same purge path, with no signal anywhere. Walk errors
  are now collected and trigger the same refusal.

Both are surfaced end to end: `error_type: CorpusRootUnavailable` from the
daemon, exit 2 from `mb index`, and a `skipped` (never `error`) nightly step —
an unreachable repository root at 03:00 must not be a reason to purge anything.

**Vendored third-party trees are no longer indexed** (`VENDORED_DIR_PATTERN`):
a directory whose name carries a version of three or more numeric components
(`renpy-8.5.2-sdk`, `node-v18.17.1-win-x64`, `jdk-17.0.2`) is an unpacked
distribution, and `SKIP_DIRS` cannot enumerate versions. Three components, not
two, because two also match hand-written `core-2.0`, `sd-1.5`, `lr-0.001`,
`api-v2` — and this rule errs *silently* when it over-matches while a miss is
visible in search results and fixed by one `code_skip_dirs` entry. Known misses
(`boost_1_82_0`, `node-v20`, `Qt5.15`) are pinned by test so the gap is not
rediscovered as a bug. Exclusions are reported **by name** — in the rebuild
stats, `mb stats`, the generation metadata and the audit tool — and written by
both the rebuild and the update path, since `_update` re-commits the loaded
metadata verbatim and a rebuild-only field would go stale forever.

New key `code_skip_dirs` (default `[]`): extra directory names pruned at any
depth, so a vendored tree the regex misses is one config entry away.

Measured on the same real root, now including this repository through a
junction: **13 repos, 440 files, 3666 chunks, 5.6 MB** (was 416 / 5775 / 8.9 MB
— the delta is 4776 SDK chunks out, ~2600 first-party chunks in), fallback
share 1.7%, 0 secret markers. `mb search "atomic commit of an index generation"
--code-only` now returns this project's own `_commit_generation` first.

Tests **1605 → 1636**.

---

# v0.5.0 — CODE-INDEX: the third corpus (C00–C07)

Closes **MB-05** (`TASK-05_Code_Indexer`, open since 2026-07-02): semantic
search over source repositories, alongside memory. Ships in the same release as
WIKI-LOOP. Full spec, decisions (`decisions/CODE-INDEX D1…D6`) and
real-machine evidence live in the vault
(`03_Tasks/SINGULARITY_CODE_INDEX_IMPLEMENTATION.md`); the user-facing guide is
[`docs/code-index.md`](docs/code-index.md).

The original 2026-07-02 spec wanted a standalone `search/code_indexer.py` with
its own scanning, mtime increment and npy+json format. WIKI-LOOP's `Corpus`
already provides all of that, so this epic **extended the shared writer instead
of duplicating it** — and in doing so surfaced two seams the spec never had.

## What shipped

- **`Corpus` is genuinely corpus-agnostic** (C01): `patterns`, `prune_dirs`,
  `skip_file_patterns`, `max_file_bytes` and a per-corpus `chunk_schema`.
  `_scan_corpus` moved from `rglob` to `os.walk` so `node_modules` is *pruned
  during traversal* rather than walked and discarded. Filter order is now a
  tested contract — directory → filename → path → `stat()` — because the real
  root offers ~111 extension-matching candidates per file actually wanted.
- **Defect fixed: `chunk_schema` was global** (D3). `_commit_generation`
  stamped `chunking.CHUNK_SCHEMA_VERSION` onto *every* corpus and `_update`
  compared against it, so bumping the markdown chunker forced a pointless full
  rebuild of the code index — and bumping the code chunker forced nothing. It
  is now a required keyword-only argument; no writer can inherit a format it
  did not produce. **Migration is a no-op by construction**: every generation on
  disk already carries the old global number, which is exactly what memory and
  sources now declare.
- **`search/code_chunking.py`** (C02): regex symbol boundaries, Python-first
  (69% of the codebase, and the one language Genevra had no chunker for) —
  methods as their own chunks, decorators pulled in, docstrings read forward
  from the signature. TS/JS/JSX ported. Ruby deliberately *not* implemented
  (D6: zero `.rb` files exist; it falls back rather than pretending).
- **`code_corpus` + `mb index rebuild|update|inspect --code`** (C03). Unset
  `code_repos_dir` is the off switch and every entry point refuses with an
  actionable message instead of writing an empty index.
- **`mb search --with-code` and `--repos`** (C04), plumbed and tested at every
  link including the cold fallback. `--code-only` stopped being a stub. Code is
  opt-in (D2), so the default result set is provably unchanged.
- **`mb nightly` step `code_index`, plus `mb stats` / `mb status` / `mb doctor`
  block `K1`** (C05). Doctor never FAILs over code — the corpus is fully
  derived, unlike `.mb/sources/` — and `mb doctor --fix --heavy` builds it.
- New config key `code_index_max_age_days` (default 7); `code_repos_dir` gained
  a `~`-expanding accessor.

## Evidence

- Live vault, C01 gate: `mb index update` after the upgrade did **not** rebuild
  (`added: 0`), `memory_index/CURRENT` unchanged, `mb curator --json` and
  `mb digest plan --json` byte-identical by SHA-256.
- Real root `D:\Project`: 13 repos, **416 files → 5775 chunks**, 8.9 MB
  embeddings, 68 s cold build, 0.5 s no-op update, warm search 15.3 ms
  (default) vs 16.8 ms (`--with-code`). Fallback share **1.6%**.
- Privacy (D5 layer three — the only layer that checks a fact): grep of the
  built `metadata.json` for `AKIA`, PEM headers, `password=`, `api_key`,
  `token=`, `Bearer`, `sk-`, `ghp_` → **0 hits**; zero `.env`/`.pem`/dump files
  indexed.
- D2 by the numbers: `--with-code` took 10 of 50 result slots and displaced
  4 notes; **top-1 changed on 0 of 10 queries**. `mb search` without flags
  matched the pre-feature baseline on all 10 queries.
  **⚠ Superseded** — re-measured after the corpus composition changed (see the
  C09/C10 section above): 18/50, 13/50 and 4/10. The favourable numbers were a
  property of that corpus, not of the design.
- Tests **1492 → 1605**.

---

# v0.5.0 — WIKI-LOOP: raw sources, ingest fan-out, intel lint (W00–W12)

Closes the gap with the LLM-wiki method: an external document is stored
immutably with provenance, and its knowledge is deliberately spread across the
existing pages it actually touches. Full spec, decisions and real-machine
evidence live in the vault (`03_Tasks/SINGULARITY_WIKI_LOOP_IMPLEMENTATION.md`,
`decisions/WIKI-LOOP D1…D7`); the user-facing guide is
[`docs/wiki-loop.md`](docs/wiki-loop.md).

## R1 — the source layer (W00–W04)

- **Decisions D1–D7 recorded before any code** (owner-signed): raw lives in
  `.mb/sources/` outside the vault tree, indexed as its own corpus, no network
  in the engine, semantics in skills, hard-deny immutability, fan-out ceiling 15.
- `singularity/sources.py` — content-addressed ids, atomic registry, write-once
  storage, Windows-safe slugs, tolerant read / strict write.
- `mb ingest add|list|show|export` with a contract exit-code scheme, zero-side-effect
  `--dry-run`, and an explicit refusal (plus the working command) for `http(s)` URLs.
- **`Corpus` abstraction in the indexer**: scanning, chunking, committing and
  locking are parameterized by (root, index_dir) instead of hardcoding the vault.
  Writer locks became per-corpus, so indexing sources never blocks the watcher.
  `mb search --with-sources` reaches the raw layer; nothing reaches it without
  the flag.
- Provenance contract: `sources:` frontmatter, `mb note new --from-source`,
  two new non-fatal `mb curator` checks, `immutable_roots` denied even in the
  guard's `audit` mode.

## R2 — the working cycle (W05, W06, W09)

- `mb ingest plan` — deterministic fan-out with mandatory `why` evidence per
  proposed page, `new_pages`, cross-corpus `conflicts`, and a refusal (rather
  than nonsense) when the two corpora were built by different models.
- Shared vector math extracted to `search/similarity.py`; proven bit-identical
  to the previous inline implementations on the live 3083-chunk index.
- `mb ingest mark` / `mb ingest log` + `.mb/ingest-log.jsonl`. A handled page
  stops resurfacing; a re-ingested (re-hashed) document brings it back.
- Skills: new `mb-ingest` (9 gated steps), `memory-curator` extended with
  adjudication and the research agenda.

## R3 — intelligence (W07, W08)

- `mb curator contradictions` — cosine band (0.72, 0.90) plus five lexical
  divergence signals, each producing evidence tokens; verdict journal so a
  dismissed false positive never returns. **Tuned on real data**: the first
  run produced 8762 candidates in 27.5 s; requiring an asserting signal,
  keying numeric mismatches, excluding file:line citations and caching
  per-chunk features brought it to 653 candidates in 1.4 s with no cap.
- `mb curator gaps` — six deterministic signals answering "what is missing",
  with true totals plus a per-signal ceiling so one broad signal cannot bury
  the rest.

## R4 — migrations, integration, release (W10–W12)

- `mb ingest backfill` — idempotent retro-provenance from `source:` frontmatter
  and URLs in `reference/` bodies.
- `schema_version` 1 → 2: 11 additive keys, no value rewriting, `.bak` before
  the migrating save.
- **`mb backup` now covers the ingest layer** — registry and every raw file with
  sha256; `restore` is strictly additive and never overwrites. This is the
  compensation the D1 storage decision owed: `.mb/sources/` is the only
  non-reconstructible data in the vault.
- `mb doctor` block W (FAIL when the registry outlives its files; `--fix` never
  deletes raw), nightly steps + trend fields, SessionStart ingest nag, sources
  sections in `mb status` / `mb stats`.
- E2E on a clean vault, and an R8 regression proving `mb uninstall`,
  `mb doctor --fix`, the documented rollback and a full reindex all leave every
  raw file byte-identical.

### Also fixed on the way

- `encoder.resolve_device()` now probes CUDA with a real kernel launch and falls
  back to CPU. `torch.cuda.is_available()` returning True on a GPU the installed
  torch has no kernels for took the whole search daemon down; the config key
  `encoder_device` overrides the probe.
- `sentence-transformers` pinned `<5` — the 5.x line imports `torchcodec`
  unconditionally, which needs FFmpeg shared libraries this project has no use for.

---

## R1 — Hook control MVP (T00–T02)

- `mb hooks status|enable|disable`, natural-language capture-hook mapping,
  `mb doctor`/`mb status` integration. (`5d5443f`, `69d2f6b`)
- Evidence baseline documenting what current upstream already had verified
  vs. what was fork-only/documentary. (`933cd92`, `docs/incoming-fork-baseline.md`)

## R2 — Multi-project foundation (T03–T05)

- Project registry (`mb project add|remove|list|detect`), deepest-path-wins
  cwd detection, reserved ids `general`/`personal`. (`9caab4e`)
- Project-aware index and search filters. (`92bbebf`)
- Project-bound capture, `mb learn --head`, bounded last-10-section dedup.
  (`a6af2c2`)

## R3 — Context and discipline (T06–T10)

- Project brain injected into SessionStart from `.mb/context.json`, budgeted
  excerpt (`brain_max_lines`). (`a975672`)
- Once-per-session module-context PreToolUse hook, opt-in
  (`module_context_enabled`). (`6d4813e`, `7027510`)
- Hook ownership detection fixed to recognize pre-marker legacy entries by
  command path, not just the `_mb` marker (real bug found registering the
  first PreToolUse hook on a live machine). (`0867045`)
- Vault guard PreToolUse hook (audit/deny modes, opt-in) + sanitized
  `config.example.json` snapshot generation. (`dddcf09`)
- `personal_roots` retrofit: the fork's blanket "everything outside `code/`
  is personal" rule was rejected; a narrower, explicit opt-in
  (`personal_roots` config + frontmatter) shipped instead. (`7267635`)
- Normalized `SessionActivity` + pure `decide()` core replacing the old raw
  tool-count/substring capture heuristic. (`37d695e`)
- Claude/Gemini capture adapters rewired onto the new core, parsed against
  REAL transcripts (not assumed) — found live that Windows Claude Code fires
  both `Bash` and `PowerShell` as distinct tool names. (`8c353f2`)

## R4 — Knowledge compilation (T11–T13)

- Digest planner: `mb digest plan|mark`, report-only merge candidates
  (learnings → reference/rule/howto/decision), atomic mark journal.
  (`1689b50`)
- **Bug found and fixed** (real-machine, not synthetic): `_writer_lock()`'s
  cleanup ran even on the losing/contended path, letting a losing writer
  delete the winning writer's active lock. (`18f0be6`)
- `mb nightly` orchestration: index update → digest/curator/config snapshot
  → trend line → markers sweep; retargets the old standalone
  `nightly-index` scheduled job; SessionStart digest-nag (threshold + 72h
  rate limit); `watch.py`'s pending-file-loss-on-lock-contention bug fixed.
  (`2c7161c`)
- Committed-index loader promoted to a shared `indexer.load_committed_index()`
  (was duplicated between `digest.py` and about to be duplicated again).
  (`477ef76`)
- Curator semantic lint: chunk-level near-duplicate detection (blockwise
  cosine, bounded memory), supersession candidates, `.mb/context.json`
  budget/reference checks. **Bug found and fixed**: `curator._note_embeddings()`
  read a stale legacy flat index path instead of the current committed
  generation — confirmed ~2 days stale on the live vault. (`ea6fe24`)

## R5 — Client-neutral workflow skills (T15)

- `mb-onboard`, `mb-triage` added as new skills; the fork's `mb-capture`/
  `mb-handoff` contracts turned out to already exist as `mb-memory`/
  `compaction-plan` — extended in place rather than duplicated.
  `install/skill_manifest.py`: single source for which client gets which
  skill, replacing two independently-maintained lists. Verified against the
  real installed Claude/Codex/Gemini clients on the development machine.
  (`9e97e63`)

## R6 — Telemetry and release gate (T14, T16)

- `mb reflect`: privacy-safe adoption telemetry — offline transcript walk,
  counts/rates only, no prompt/tool-argument/session-id/transcript-path
  persistence ever. Transcript locations and wire formats (including
  subagent exclusion via `isSidechain`) verified against real Claude Code
  and Gemini CLI session data, not documentation. (`c7b9119`)
- Release gate (this task): fixture-matrix E2E coverage (legacy config,
  malformed settings, foreign hooks, full install→status→disable→enable→
  doctor→uninstall lifecycle, SessionStart-independent-of-`auto_capture`
  proof, general-classification/no-file-move proof), measured performance
  numbers, a cross-cutting privacy audit, and this documentation pass
  (README, [UPGRADE.md](docs/UPGRADE.md), [ROLLBACK.md](docs/ROLLBACK.md)).

## Known open items (not silently claimed as done)

- Codex has an install phase (`AGENTS.md` profile + skill junctions) but no
  `mb doctor` health check and no runtime hook, unlike Gemini — see
  README's "Other CLI clients" section.
- `mb reflect`'s `eligible_research`/`code_edits` classification for Gemini
  sessions is cruder than Claude's (no per-event cwd boundary in the
  verified transcript shape) — documented as a limitation in
  `singularity/reflect.py`, not silently assumed equivalent.
