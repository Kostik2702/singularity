"""RAGAS-style context metrics for `mb search`, scored from a snapshot.

    # 1. take a snapshot of any engine state (or any command with the mb --json shape)
    python tools/rag_eval_snapshot.py --set eval_set.json --out snap.json [--cmd '...']
    # 2. score it
    python tools/rag_eval.py score --set eval_set.json --snap snap.json --out scores.json \
        [--judgments judgments.json]
    # 3. compare two scored runs
    python tools/rag_eval.py compare base_scores.json new_scores.json
    # one-shot: snapshot + score
    python tools/rag_eval.py run --set eval_set.json --out-dir runs/after-change [--cmd '...']
    # LLM-judge worksheet for chunks/facts the anchors cannot decide
    python tools/rag_eval.py judge-sheet --set eval_set.json --snap snap.json --ids ids.txt --out sheet.md

The eval set is machine-private (it quotes vault notes), so it is never in the repo. By
convention it lives in the vault's local state, e.g. <vault>/.mb/eval/<name>/eval_set.json.

DEFINITIONS (all at K=5 unless said otherwise; top-5 = the `--top 5` run, R@10 = the `--top 10` run)

  relevant chunk, STRICT  : the chunk's content contains the verbatim anchor of at least one
                            reference fact of the case (normalised: lower case, the Russian letter yo folded to ye, *`_ removed,
                            whitespace collapsed). This is "the chunk carries part of the answer".
  relevant chunk, LENIENT : the chunk comes from a file listed in `relevant`.
  context_precision       : RAGAS LLMContextPrecisionWithReference formula,
                            sum_k(precision@k * v_k) / (#relevant in top K), 0 if none relevant,
                            with v_k = STRICT relevance (or the judge's verdict when judged).
  context_recall          : RAGAS LLMContextRecall formula, |reference claims attributable to the
                            retrieved contexts| / |reference claims|. A claim is attributable when its
                            anchor is in a top-K chunk (or the judge says so for a paraphrase).
  noise@5                 : share of returned top-5 chunks that are NOT relevant (strict / lenient).
                            Denominator is what came back, so a 2-result answer is not rewarded.
  file R@k / MRR          : as tools/search_eval.py — results collapsed to files, first occurrence;
                            hit = any relevant file. For `multi` also all_required@5 = every
                            role=required/primary file is in the top-5 files.
  section_hit@5           : a top-5 chunk from the primary file with the expected section heading.
  unanswerable            : how many results came back and their max score. Ideal = 0 results.
  AUC                     : Mann-Whitney AUC of the top-1 score, positive = top-1 chunk STRICT-
                            relevant, negative = top-1 irrelevant (incl. every unanswerable case);
                            plus answerable-vs-unanswerable on the top-1 score. 0.5 = the score
                            carries no information for a refusal threshold.

Cases whose target is not in the index (`target_not_indexed`) are reported separately and are
excluded from the headline, so the headline measures ranking, not indexing coverage.

EVAL SET SHAPE (what the scripts read; one JSON file)

  {"cases": [ <case>, ... ]}              top level: a "cases" list, nothing else is read

  case = {
    "id":       "q-017",                  unique; the snapshot and the judgments are keyed by it
    "class":    "natural",                identifier | natural | crosslingual | multi | unanswerable
    "query":    "how do I rotate the API key",
    "origin":   "new",                    optional; "new" / "legacy" only feeds the by_origin split
    "answer":   "Run the rotate script, then restart the worker.",
                                          optional; the reference answer, shown in judge-sheet
    "relevant": [                         required for every class ([] for unanswerable)
      {"file": "howto/Rotate the API key.md",   path as `mb search` reports it (meta.file)
       "section": "Steps",                      optional; heading, used for section_hit@5 on the primary
       "role": "primary"}                       primary | required | other; primary and required are the
    ],                                          files all_required@5 wants (a `multi` case needs at least one)
    "facts": [                            optional; absent or empty = the case has no strict metrics
      {"claim": "The old key stays valid for one hour.",   one reference claim
       "anchor": "valid for one hour"}                     verbatim substring of the chunk that carries it
    ],
    "target_not_indexed": false           optional; true = reported separately, kept out of the headline
  }

An unanswerable case has "relevant": [] and no facts; the ideal answer to it is no results.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
SET_HELP = ("eval set JSON; machine-private, lives in the vault's local state, "
            "e.g. <vault>/.mb/eval/search-eval-v2/eval_set_v2.json")
CLASSES = ("identifier", "natural", "crosslingual", "multi", "unanswerable")


def norm(s: str) -> str:
    s = (s or "").lower().replace("\u0451", "\u0435")
    s = re.sub(r"[*`_]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def chunk_key(case_id: str, item: dict) -> str:
    """Judgments are keyed by case + chunk TEXT, so a re-chunked engine re-asks only for new text."""
    content = (item.get("meta") or {}).get("content", "")
    return f"{case_id}::{hashlib.sha1(norm(content).encode()).hexdigest()[:16]}"


def ranked_files(results: list[dict]) -> list[str]:
    seen: list[str] = []
    for it in results:
        f = (it.get("meta") or {}).get("file")
        if f and f not in seen:
            seen.append(f)
    return seen


def ragas_context_precision(verdicts: list[int]) -> float:
    rel = sum(verdicts)
    if rel == 0:
        return 0.0
    acc, hits = 0.0, 0
    for k, v in enumerate(verdicts, 1):
        if v:
            hits += 1
            acc += hits / k
    return acc / rel


def auc(pos: list[float], neg: list[float]) -> float | None:
    if not pos or not neg:
        return None
    wins = 0.0
    for p in pos:
        for n in neg:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(pos) * len(neg))


def best_threshold(pos: list[float], neg: list[float]) -> dict | None:
    """The cut on top-1 score maximising balanced accuracy, with what it costs."""
    if not pos or not neg:
        return None
    best = None
    for t in sorted(set(pos + neg)):
        tpr = sum(p >= t for p in pos) / len(pos)
        tnr = sum(n < t for n in neg) / len(neg)
        cand = (0.5 * (tpr + tnr), t, tpr, tnr)
        if best is None or cand > best:
            best = cand
    ba, t, tpr, tnr = best
    return {"threshold": round(t, 4), "balanced_acc": round(ba, 3),
            "keeps_relevant": round(tpr, 3), "refuses_irrelevant": round(tnr, 3)}


def load_judgments(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {"chunks": {}, "facts": {}}
    j = json.loads(Path(path).read_text())
    return {"chunks": j.get("chunks", {}), "facts": j.get("facts", {})}


def score_case(case: dict, run: dict, judg: dict) -> dict:
    res5 = (run.get("top5") or {}).get("results") or []
    res10 = (run.get("top10") or {}).get("results") or []
    rel_files = [r["file"] for r in case["relevant"]]
    req_files = [r["file"] for r in case["relevant"] if r.get("role") in ("primary", "required")]
    anchors = [norm(f["anchor"]) for f in case.get("facts", [])]
    contents = [norm((it.get("meta") or {}).get("content", "")) for it in res5]
    files5 = [(it.get("meta") or {}).get("file") for it in res5]
    scores5 = [float(it.get("score", 0.0)) for it in res5]

    strict = [int(any(a in c for a in anchors)) for c in contents]
    lenient = [int(f in rel_files) for f in files5]
    out = {"id": case["id"], "class": case["class"], "origin": case.get("origin"),
           "n_results": len(res5), "top1_score": scores5[0] if scores5 else None,
           "max_score": max(scores5) if scores5 else None}

    if case["class"] == "unanswerable":
        out.update({"noise_strict": 1.0 if res5 else None, "noise_lenient": 1.0 if res5 else None,
                    "top1_relevant": 0})
        jv = [judg["chunks"].get(chunk_key(case["id"], it)) for it in res5]
        if res5 and all(v is not None for v in jv):
            out["judged"] = True
            out["judge_any_answer"] = int(any(jv))
        return out

    # judged verdicts override STRICT where present
    jv = [judg["chunks"].get(chunk_key(case["id"], it)) for it in res5]
    judged_chunks = bool(res5) and all(v is not None for v in jv)
    fact_verdicts = []
    for i, a in enumerate(anchors):
        hit = any(a in c for c in contents)
        jf = judg["facts"].get(f"{case['id']}::{i}")
        fact_verdicts.append((hit, jf))

    out["has_facts"] = bool(anchors)
    if anchors:
        out["context_precision"] = ragas_context_precision(strict)
        out["context_recall"] = sum(h for h, _ in fact_verdicts) / len(anchors)
        out["noise_strict"] = (1 - sum(strict) / len(strict)) if strict else None
        out["top1_relevant"] = strict[0] if strict else 0
    out["noise_lenient"] = (1 - sum(lenient) / len(lenient)) if lenient else None
    out["cp_lenient"] = ragas_context_precision(lenient)
    if judged_chunks:
        v = [int(bool(x)) for x in jv]
        out["judged"] = True
        out["judge_context_precision"] = ragas_context_precision(v)
        out["judge_noise"] = 1 - sum(v) / len(v)
        out["judge_top1_relevant"] = v[0]
        if anchors and all(h or jf is not None for h, jf in fact_verdicts):
            out["judge_context_recall"] = sum(1 for h, jf in fact_verdicts if h or jf) / len(anchors)

    rank5 = ranked_files(res5)
    rank10 = ranked_files(res10) or rank5
    def first(ranking):
        for i, f in enumerate(ranking, 1):
            if f in rel_files:
                return i
        return None
    r5, r10 = first(rank5), first(rank10)
    out["file_rank_top5run"] = r5
    out["file_rank_top10run"] = r10
    out["R@1"] = int(r5 == 1)
    out["R@5"] = int(r5 is not None)
    out["R@10"] = int(r10 is not None)
    out["MRR"] = 1.0 / r10 if r10 else 0.0
    if case["class"] == "multi":
        out["all_required@5"] = int(all(f in rank5 for f in req_files))
        out["required_coverage@5"] = sum(f in rank5 for f in req_files) / len(req_files)
    prim = [r for r in case["relevant"] if r.get("role") == "primary" and r.get("section")]
    if prim:
        p = prim[0]
        out["section_hit@5"] = int(any((it.get("meta") or {}).get("file") == p["file"] and
                                       norm((it.get("meta") or {}).get("section", "")) == norm(p["section"])
                                       for it in res5))
    return out


def aggregate(rows: list[dict]) -> dict:
    keys = ["R@1", "R@5", "R@10", "MRR", "context_precision", "context_recall", "noise_strict",
            "noise_lenient", "cp_lenient", "section_hit@5", "all_required@5", "required_coverage@5",
            "judge_context_precision", "judge_context_recall", "judge_noise", "n_results"]
    agg = {"n": len(rows)}
    for k in keys:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if vals:
            agg[k] = round(mean(vals), 3)
            agg[f"{k}__n"] = len(vals)
    un = [r for r in rows if r["class"] == "unanswerable"]
    if un:
        ms = [r["max_score"] for r in un if r["max_score"] is not None]
        agg["unans_returned_any"] = round(sum(r["n_results"] > 0 for r in un) / len(un), 3)
        agg["unans_mean_results"] = round(mean(r["n_results"] for r in un), 2)
        if ms:
            ms_sorted = sorted(ms)
            agg["unans_max_score_mean"] = round(mean(ms), 4)
            agg["unans_max_score_median"] = round(ms_sorted[len(ms) // 2], 4)
            agg["unans_max_score_max"] = round(max(ms), 4)
    return agg


def separation(rows: list[dict]) -> dict:
    ans = [r for r in rows if r["class"] != "unanswerable" and r.get("has_facts") and r["top1_score"] is not None]
    un = [r for r in rows if r["class"] == "unanswerable" and r["top1_score"] is not None]
    pos = [r["top1_score"] for r in ans if r.get("top1_relevant")]
    neg = [r["top1_score"] for r in ans if not r.get("top1_relevant")] + [r["top1_score"] for r in un]
    a_scores = [r["top1_score"] for r in ans]
    u_scores = [r["top1_score"] for r in un]
    out = {"auc_top1_relevant_vs_not": auc(pos, neg), "n_pos": len(pos), "n_neg": len(neg),
           "threshold_top1_relevant": best_threshold(pos, neg),
           "auc_answerable_vs_unanswerable": auc(a_scores, u_scores),
           "threshold_answerable_vs_unanswerable": best_threshold(a_scores, u_scores),
           "n_answerable": len(a_scores), "n_unanswerable": len(u_scores)}
    by = {}
    for cls in CLASSES[:-1]:
        p = [r["top1_score"] for r in ans if r["class"] == cls and r.get("top1_relevant")]
        n = [r["top1_score"] for r in ans if r["class"] == cls and not r.get("top1_relevant")]
        by[cls] = auc(p, n + u_scores)
    out["auc_by_class_vs_irrelevant_plus_unans"] = by
    for k, v in list(out.items()):
        if isinstance(v, float):
            out[k] = round(v, 3)
    out["auc_by_class_vs_irrelevant_plus_unans"] = {k: (round(v, 3) if v is not None else None) for k, v in by.items()}
    return out


def bootstrap_ci(rows: list[dict], keys: tuple[str, ...], n: int = 2000, seed: int = 11) -> dict:
    """95% percentile bootstrap CI over cases: how far a class mean can move by sampling alone."""
    import random
    rng = random.Random(seed)
    out = {}
    for k in keys:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if len(vals) < 5:
            continue
        means = sorted(mean(rng.choices(vals, k=len(vals))) for _ in range(n))
        out[k] = [round(means[int(0.025 * n)], 3), round(means[int(0.975 * n) - 1], 3)]
    return out


def judge_calibration(ds: dict, snap: dict, judg: dict) -> dict | None:
    """How far the cheap proxies (STRICT anchor, LENIENT file) agree with the judge, and how well a
    chunk's own score separates judge-relevant from judge-irrelevant chunks (the per-chunk cut a
    context filter would make). Only cases whose every top-5 chunk has a verdict are used."""
    if not judg["chunks"]:
        return None
    rows = []
    for case in ds["cases"]:
        run = snap["runs"].get(case["id"])
        res = ((run or {}).get("top5") or {}).get("results") or []
        keys = [chunk_key(case["id"], it) for it in res]
        if not res or any(k not in judg["chunks"] for k in keys):
            continue
        anchors = [norm(f["anchor"]) for f in case.get("facts", [])]
        rel = [r["file"] for r in case["relevant"]]
        for it, k in zip(res, keys, strict=True):
            m = it.get("meta") or {}
            rows.append({"cls": case["class"], "score": float(it.get("score", 0.0)),
                         "strict": int(any(a in norm(m.get("content", "")) for a in anchors)),
                         "lenient": int(m.get("file") in rel), "judge": int(bool(judg["chunks"][k]))})
    if not rows:
        return None
    def agree(key):
        tp = sum(r[key] and r["judge"] for r in rows); fp = sum(r[key] and not r["judge"] for r in rows)
        fn = sum((not r[key]) and r["judge"] for r in rows)
        return {"precision_vs_judge": round(tp / (tp + fp), 3) if tp + fp else None,
                "recall_vs_judge": round(tp / (tp + fn), 3) if tp + fn else None}
    pos = [r["score"] for r in rows if r["judge"]]
    neg = [r["score"] for r in rows if not r["judge"]]
    ans_rows = [r for r in rows if r["cls"] != "unanswerable"]
    return {"n_chunks": len(rows), "judge_relevant_share": round(len(pos) / len(rows), 3),
            "strict_anchor": agree("strict"), "lenient_file": agree("lenient"),
            "chunk_score_auc_judge": round(auc(pos, neg), 3) if auc(pos, neg) is not None else None,
            "chunk_score_auc_judge_answerable_only": round(auc([r["score"] for r in ans_rows if r["judge"]],
                                                                [r["score"] for r in ans_rows if not r["judge"]]), 3),
            "chunk_threshold": best_threshold(pos, neg)}


def score(set_path: str, snap_path: str, judg_path: str | None) -> dict:
    ds = json.loads(Path(set_path).read_text())
    snap = json.loads(Path(snap_path).read_text())
    judg = load_judgments(judg_path)
    rows, missing = [], []
    for case in ds["cases"]:
        run = snap["runs"].get(case["id"])
        if run is None:
            missing.append(case["id"])
            continue
        r = score_case(case, run, judg)
        r["target_not_indexed"] = bool(case.get("target_not_indexed"))
        rows.append(r)
    head = [r for r in rows if not r["target_not_indexed"]]
    by_class = {c: aggregate([r for r in head if r["class"] == c]) for c in CLASSES}
    by_origin = {o: aggregate([r for r in head if r["origin"] == o]) for o in ("new", "legacy")}
    legacy_all = aggregate([r for r in rows if r["origin"] == "legacy"])
    answerable = aggregate([r for r in head if r["class"] != "unanswerable"])
    judged = [r for r in head if r.get("judged")]
    return {"set": set_path, "snapshot": snap_path, "snapshot_meta": snap.get("meta"),
            "judgments": judg_path, "missing_in_snapshot": missing,
            "headline_answerable": answerable, "by_class": by_class, "by_origin": by_origin,
            "ci95": {c: bootstrap_ci([r for r in head if (c == "ANSWERABLE" and r["class"] != "unanswerable") or r["class"] == c],
                                     ("R@1", "R@5", "MRR", "context_precision", "context_recall", "noise_strict",
                                      "judge_context_precision", "judge_context_recall", "judge_noise"))
                     for c in list(CLASSES[:-1]) + ["ANSWERABLE"]},
            "legacy_including_unindexed_targets": legacy_all,
            "not_indexed_cases": [r["id"] for r in rows if r["target_not_indexed"]],
            "judged": {"n_cases": len(judged), "by_class": {c: aggregate([r for r in judged if r["class"] == c]) for c in CLASSES},
                       "all_answerable": aggregate([r for r in judged if r["class"] != "unanswerable"]),
                       "calibration": judge_calibration(ds, snap, judg),
                       "separation_judge": separation([dict(r, top1_relevant=r.get("judge_top1_relevant", r.get("top1_relevant")))
                                                       for r in judged])},
            "separation": separation(head), "rows": rows}


def fmt_table(sc: dict) -> str:
    cols = ["n", "R@1", "R@5", "R@10", "MRR", "context_precision", "context_recall", "noise_strict",
            "noise_lenient", "section_hit@5", "n_results"]
    lines = ["class        " + " ".join(f"{c[:10]:>10}" for c in cols)]
    for name, agg in list(sc["by_class"].items()) + [("ANSWERABLE", sc["headline_answerable"])]:
        lines.append(f"{name:<12} " + " ".join(f"{agg.get(c, ''):>10}" for c in cols))
    u = sc["by_class"]["unanswerable"]
    lines.append(f"unanswerable: returned_any={u.get('unans_returned_any')} mean_results={u.get('unans_mean_results')} "
                 f"max_score mean/median/max={u.get('unans_max_score_mean')}/{u.get('unans_max_score_median')}/{u.get('unans_max_score_max')}")
    s = sc["separation"]
    lines.append(f"AUC top1 relevant-vs-not={s['auc_top1_relevant_vs_not']} (pos {s['n_pos']}, neg {s['n_neg']}); "
                 f"answerable-vs-unanswerable={s['auc_answerable_vs_unanswerable']}")
    lines.append(f"best threshold (relevant-vs-not): {s['threshold_top1_relevant']}")
    return "\n".join(lines)


def cmd_compare(a: str, b: str) -> None:
    A, B = json.loads(Path(a).read_text()), json.loads(Path(b).read_text())
    keys = ["R@1", "R@5", "R@10", "MRR", "context_precision", "context_recall", "noise_strict", "noise_lenient"]
    for cls in list(CLASSES) + ["ANSWERABLE"]:
        x = A["headline_answerable"] if cls == "ANSWERABLE" else A["by_class"][cls]
        y = B["headline_answerable"] if cls == "ANSWERABLE" else B["by_class"][cls]
        print(cls, " ".join(f"{k}:{x.get(k)}->{y.get(k)}" for k in keys if k in x or k in y))
    ua, ub = A["by_class"]["unanswerable"], B["by_class"]["unanswerable"]
    print("unanswerable returned_any", ua.get("unans_returned_any"), "->", ub.get("unans_returned_any"),
          "max_score_mean", ua.get("unans_max_score_mean"), "->", ub.get("unans_max_score_mean"))
    print("AUC", A["separation"]["auc_top1_relevant_vs_not"], "->", B["separation"]["auc_top1_relevant_vs_not"])
    ra = {r["id"]: r for r in A["rows"]}
    flips = [(r["id"], ra[r["id"]].get("context_recall"), r.get("context_recall")) for r in B["rows"]
             if r["id"] in ra and r.get("context_recall") is not None and ra[r["id"]].get("context_recall") != r.get("context_recall")]
    print(f"cases whose context_recall changed: {len(flips)}")


def cmd_judge_sheet(set_path: str, snap_path: str, ids_path: str, out: str, chars: int) -> None:
    ds = {c["id"]: c for c in json.loads(Path(set_path).read_text())["cases"]}
    snap = json.loads(Path(snap_path).read_text())["runs"]
    ids = [line.strip() for line in Path(ids_path).read_text().splitlines() if line.strip()]
    w = []
    for cid in ids:
        c, res = ds[cid], (snap[cid].get("top5") or {}).get("results") or []
        anchors = [norm(f["anchor"]) for f in c.get("facts", [])]
        w.append(f"### {cid} [{c['class']}] Q: {c['query']}\nREF: {c.get('answer')}")
        for i, f in enumerate(c.get("facts", [])):
            hit = any(norm(f['anchor']) in norm((it.get('meta') or {}).get('content', '')) for it in res)
            w.append(f"  F{i} {'HIT' if hit else 'miss'}: {f['claim']}")
        want = set(re.findall(r"\w{4,}", norm((c.get("answer") or "") + " " + c["query"])))
        for k, it in enumerate(res, 1):
            m = it.get("meta") or {}
            text = norm(m.get("content", ""))
            s = int(any(a in text for a in anchors))
            head = f"  [{k}] s={it.get('score')} strict={s} key={chunk_key(cid, it)} {m.get('file')} § {m.get('section')}"
            if s:
                w.append(head + "  (anchor verbatim -> relevant)")
                continue
            # show the window of the chunk that overlaps the reference most, not just its head
            best, pos = -1, 0
            for i in range(0, max(1, len(text) - chars + 1), max(1, chars // 3)):
                ov = len(want & set(re.findall(r"\w{4,}", text[i:i + chars])))
                if ov > best:
                    best, pos = ov, i
            w.append(head + f" len={len(text)} @{pos}\n      {text[pos:pos + chars]}")
    Path(out).write_text("\n".join(w))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action", required=True)
    s = sub.add_parser("score"); s.add_argument("--set", required=True, help=SET_HELP); s.add_argument("--snap", required=True)
    s.add_argument("--out", required=True); s.add_argument("--judgments")
    r = sub.add_parser("run"); r.add_argument("--set", required=True, help=SET_HELP); r.add_argument("--out-dir", required=True)
    r.add_argument("--cmd", default=None); r.add_argument("--judgments"); r.add_argument("--jobs", default="4")
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    j = sub.add_parser("judge-sheet"); j.add_argument("--set", required=True, help=SET_HELP); j.add_argument("--snap", required=True)
    j.add_argument("--ids", required=True); j.add_argument("--out", required=True); j.add_argument("--chars", type=int, default=600)
    a = ap.parse_args()
    if a.action == "compare":
        cmd_compare(a.a, a.b); return 0
    if a.action == "judge-sheet":
        cmd_judge_sheet(a.set, a.snap, a.ids, a.out, a.chars); return 0
    if a.action == "run":
        od = Path(a.out_dir); od.mkdir(parents=True, exist_ok=True)
        argv = [sys.executable, str(HERE / "rag_eval_snapshot.py"), "--set", a.set, "--out", str(od / "snapshot.json"), "--jobs", a.jobs]
        if a.cmd:
            argv += ["--cmd", a.cmd]
        rc = subprocess.run(argv).returncode
        if rc not in (0, 2):
            return rc
        snap, out = str(od / "snapshot.json"), str(od / "scores.json")
    else:
        snap, out = a.snap, a.out
    sc = score(a.set, snap, a.judgments)
    Path(out).write_text(json.dumps(sc, ensure_ascii=False, indent=1))
    print(fmt_table(sc))
    print(f"-> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
