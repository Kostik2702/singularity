# Measuring retrieval quality (`tools/rag_eval.py`)

`tools/rag_eval.py` scores `mb search` against an eval set: a JSON file of queries,
each with the notes and facts that answer it. The shape of the file is described at
the end of the module docstring (`EVAL SET SHAPE`). The scorer never calls the engine
itself; `tools/rag_eval_snapshot.py` runs every query and stores the raw answers, so a
baseline frozen today can be scored again tomorrow, and a changed engine is measured
by taking a new snapshot of the same set.

## What it measures

All metrics are at K=5 unless noted; the top-10 run feeds R@10.

- **R@1 / R@5 / R@10 / MRR**: results collapsed to files; a hit is any relevant file.
- **context precision**: the RAGAS formula over the top-5 chunks. A chunk counts as relevant when it contains the verbatim anchor of a reference fact.
- **context recall**: the share of reference claims whose anchor appears in a top-5 chunk.
- **noise@5**: the share of returned chunks that are not relevant (strict by anchor, lenient by file).
- **section_hit@5**, **all_required@5**: the expected section is in the top 5; for `multi` cases, every required file is.
- **unanswerable**: how many results came back for a query with no answer in the vault. The ideal is none.
- **AUC**: how well the top-1 score separates a relevant answer from an irrelevant one (0.5 = no information).

Cases whose target is not indexed are reported separately and kept out of the headline.

## Commands

```
python tools/rag_eval.py run --set eval_set.json --out-dir runs/after-change [--cmd '...']
python tools/rag_eval.py score --set eval_set.json --snap snap.json --out scores.json
python tools/rag_eval.py compare base_scores.json new_scores.json
python tools/rag_eval.py judge-sheet --set eval_set.json --snap snap.json --ids ids.txt --out sheet.md
```

- `run` takes the snapshot and scores it in one step.
- `score` followed by `compare` scores two snapshots and prints the per-class difference.
- `judge-sheet` writes the chunks the anchors cannot decide, for an LLM or a person to judge; the verdicts go back in through `--judgments`.

The command under test is a template with `{query}` and `{top}`. The recommended one
measures the raw ranking:

```
mb search {query} --top {top} --json --full --no-jev
```

The default, used when `--cmd` is omitted, is `mb search {query} --top {top} --json --full`,
which includes the Jev relevance filter when Jev is enabled.

## The eval set is private

The set quotes vault notes (queries, anchors, reference answers), so it is private to
the machine and never goes into the repository. By convention it lives in the vault's
local state, for example `<vault>/.mb/eval/search-eval-v2/eval_set_v2.json`. Build your
own from the shape in the docstring; include `unanswerable` cases.

## Reference numbers

The engine's measured baseline on the owner's vault (346 queries, 298 answerable):

| | R@1 | R@5 | context precision | context recall | unanswerable answered with nothing |
|---|---|---|---|---|---|
| default pipeline | 0.775 | 0.933 | 0.681 | 0.744 | 73 % |
| `--no-jev` | 0.752 | 0.936 | | | |

Your vault will give other numbers; compare the same set before and after a change,
not against this table.

## A caution on set size

A set under ~100 queries has a spread of about ±12 pp; three conclusions drawn from a
66-query set were overturned by a 346-query one. Decide on retrieval changes only from
a set of a few hundred queries with unanswerable cases included.
