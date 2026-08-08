"""W07: contradiction candidates — signals, evidence, band, journal.

The detector's promise is narrow and must be kept exactly: every candidate
carries the tokens that triggered it, near-duplicates stay out (curator.lint
owns those), and a dismissed pair never comes back.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from singularity.knowledge import contradictions
from singularity.cli import main
from singularity.config import Config
from singularity.search import indexer


@pytest.fixture
def cfg(tmp_path):
    c = Config(vault=tmp_path, data={})
    c.mb_dir.mkdir(parents=True, exist_ok=True)
    return c


# --- signals ----------------------------------------------------------------

def test_polarity_flip_is_found_with_evidence():
    result = contradictions.evaluate(
        "The daemon is always started at session start.",
        "The daemon is never started at session start.", 0.8)
    assert result is not None
    score, evidence = result
    assert "polarity_flip" in evidence
    assert "always<->never" in evidence["polarity_flip"]
    assert 0 < score <= 0.8


#: The lexical signals read English words and say so; the STRUCTURAL ones read
#: shapes and must hold in every language. These are the pairs that prove the
#: split is real rather than a claim in a comment — if someone reintroduces an
#: alphabet range into the key regex, exactly these go red.
STRUCTURAL_IN_OTHER_LANGUAGES = [
    ("демон: true в конфиге", "демон: false в конфиге", "демон"),
    ("デーモン: true", "デーモン: false", "デーモン"),
    ("δαίμονας: true", "δαίμονας: false", "δαίμονας"),
]


@pytest.mark.parametrize("left,right,key", STRUCTURAL_IN_OTHER_LANGUAGES)
def test_boolean_flip_holds_in_any_script(left, right, key):
    result = contradictions.evaluate(left, right, 0.8)
    assert result is not None, f"{key}: a boolean flip went unseen"
    assert result[1]["boolean_flip"] == [f"{key}: [True] vs [False]"]


def test_numeric_mismatch_holds_in_any_script():
    _, evidence = contradictions.evaluate("порог: 60s", "порог: 90s", 0.8)
    assert set(evidence["numeric_mismatch"]) == {"порог=60s", "порог=90s"}


def test_numeric_mismatch_needs_the_same_key():
    _, evidence = contradictions.evaluate(
        "The build requires python 3.11 to run.",
        "The build requires python 3.13 to run.", 0.8)
    assert set(evidence["numeric_mismatch"]) == {"python=3.11", "python=3.13"}


def test_numeric_mismatch_ignores_unrelated_numbers():
    """A bare 'the texts contain different numbers' test fires on almost any
    pair — the key must match."""
    result = contradictions.evaluate(
        "The report has 5 sections about corpus indexing.",
        "The appendix has 9 tables about corpus indexing.", 0.8)
    assert result is None or "numeric_mismatch" not in result[1]


def test_trailing_zeros_are_not_a_disagreement():
    result = contradictions.evaluate("denoise: 0.20 in the recipe",
                                     "denoise: 0.2 in the recipe", 0.8)
    assert result is None or "numeric_mismatch" not in result[1]


def test_file_line_citations_are_not_values():
    """`digest.py:36` vs `digest.py:133` is a code pointer, not a claim."""
    result = contradictions.evaluate(
        "See digest.py: 36 for the journal.",
        "See digest.py: 133 for compatibility.", 0.8)
    assert result is None or "numeric_mismatch" not in result[1]


def test_boolean_flip_needs_disjoint_values():
    """A chunk documenting a before/after holds BOTH values; comparing it
    against either side is not a contradiction."""
    result = contradictions.evaluate("was ensure_ascii: false, now ensure_ascii: true",
                                     "ensure_ascii: false in the config", 0.8)
    assert result is None or "boolean_flip" not in result[1]


def test_numeric_mismatch_with_units():
    _, evidence = contradictions.evaluate(
        "retention 60d by default", "retention 90d by default", 0.8)
    assert set(evidence["numeric_mismatch"]) == {"retention=60d", "retention=90d"}


def test_boolean_flip():
    _, evidence = contradictions.evaluate(
        "vault_guard_enabled: true in the config",
        "vault_guard_enabled: false in the config", 0.8)
    assert evidence["boolean_flip"] == [
        "vault_guard_enabled: [True] vs [False]"]


def test_the_same_sentence_negated_is_a_strong_signal():
    """`negation_flip`: identical wording, one side negated."""
    _, evidence = contradictions.evaluate(
        "The hook blocks a write into a managed root.",
        "The hook does not block a write into a managed root.", 0.8)
    assert "not" in evidence["negation_flip"]
    assert any(t.startswith("word_overlap=") for t in evidence["negation_flip"])


def test_incidental_negation_is_only_a_modifier_never_a_trigger():
    """Measured on the real vault: a one-sided negation in otherwise different
    text fires on almost any long/short pair. It may add weight, never create
    a candidate on its own."""
    result = contradictions.evaluate(
        "The indexer reads the committed generation and collects chunks from the "
        "vault under the skip_dirs rules, then writes a generation.",
        "The daemon answers search requests, it does not load the model twice "
        "and keeps the corpora in memory between calls.", 0.8)
    assert result is None


def test_negation_on_both_sides_is_not_asymmetry():
    both = contradictions.evaluate(
        "The hook does not block writes.", "The hook does not block reads.", 0.8)
    assert both is None or not ({"negation_flip", "negation_asymmetry"} & set(both[1]))


def test_supersede_marker_is_directional():
    _, evidence = contradictions.evaluate(
        "This approach is deprecated, use the new one.",
        "Use this approach when building.", 0.8)
    assert "deprecated" in evidence["supersede_marker"]

    both = contradictions.evaluate("deprecated approach", "deprecated too", 0.8)
    assert both is None or "supersede_marker" not in both[1]


def test_no_signal_means_no_candidate():
    assert contradictions.evaluate(
        "The indexer reads the committed generation.",
        "The indexer reads the recorded generation of the index.", 0.85) is None


def test_score_scales_with_cosine_and_signal_mass():
    strong, _ = contradictions.evaluate("xkey: true", "xkey: false", 0.8)
    weak, _ = contradictions.evaluate(
        "The rule is enforced for everyone.",
        "The rule is not enforced for everyone.", 0.8)
    assert strong > weak
    lower, _ = contradictions.evaluate("xkey: true", "xkey: false", 0.75)
    assert lower < strong


def test_same_file_pairs_need_a_strong_signal(tmp_path):
    """Inside one note a differing number is nearly always a comparison table
    or a versioned recipe (v3 next to v4) — a juxtaposition, not a conflict."""
    a, b = "denoise: 0.18 in the recipe", "denoise: 0.22 in the recipe"
    assert contradictions.evaluate(a, b, 0.8) is not None          # cross-file: reported
    assert contradictions.evaluate(a, b, 0.8, same_file=True) is None
    # a strong signal still gets through inside one file
    assert contradictions.evaluate("the flag is always enabled",
                                   "the flag is never enabled",
                                   0.8, same_file=True) is not None


def test_word_boundaries_prevent_substring_hits():
    """`not` must not match inside `notation`, `no` not inside `nobody`."""
    result = contradictions.evaluate(
        "The notation took a week of corpus indexing.",
        "Indexing the corpus took a great deal of time.", 0.8)
    assert result is None or "negation_asymmetry" not in result[1]


# --- pair key ---------------------------------------------------------------

def test_pair_key_is_order_independent():
    a = contradictions.pair_key("a.md", "S1", "b.md", "S2")
    b = contradictions.pair_key("b.md", "S2", "a.md", "S1")
    assert a == b


# --- journal ----------------------------------------------------------------

def test_verdict_round_trip(cfg):
    key = contradictions.pair_key("a.md", "S", "b.md", "S")
    out = contradictions.record_verdict(cfg, key, "false_positive", note="different scope")
    assert out["total"] == 1
    stored = contradictions.read_journal(cfg)["verdicts"][key]
    assert stored["verdict"] == "false_positive" and stored["note"] == "different scope"


def test_unknown_verdict_is_rejected(cfg):
    with pytest.raises(ValueError):
        contradictions.record_verdict(cfg, "k", "maybe")


def test_damaged_journal_reads_as_empty(cfg):
    contradictions.journal_path(cfg).write_text("{broken", encoding="utf-8")
    assert contradictions.read_journal(cfg)["verdicts"] == {}


# --- scan over a real index -------------------------------------------------

class FakeEncoder:
    """Embeds by TOKEN OVERLAP so planted pairs land inside the band instead of
    at a random hash distance."""

    model_name = "fake"
    # The discriminating words are IN the vocabulary on purpose: without them
    # a planted pair embeds identically (cosine 1.0) and falls outside any
    # band, which would make the test pass for the wrong reason.
    VOCAB = ["daemon", "session", "index", "corpus", "rule", "config",
             "python", "build", "watcher", "login",
             "always", "never", "3.11", "3.13", "enforced"]

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), len(self.VOCAB) + 1), dtype=np.float32)
        for i, text in enumerate(texts):
            low = text.lower()
            vec = np.array([low.count(w) for w in self.VOCAB] + [1.0], dtype=np.float32)
            out[i] = vec / np.linalg.norm(vec)
        return out


def _vault(tmp_path, monkeypatch, pairs):
    monkeypatch.setattr(indexer, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    vault = tmp_path / "vault"
    (vault / "reference").mkdir(parents=True)
    for name, body in pairs:
        (vault / "reference" / name).write_text(
            f"---\ntype: reference\ncreated: 2026-01-01\ntitle: {name}\n---\n\n"
            f"# {name}\n\n## Rule\n{body}\n", encoding="utf-8")
    cfg = Config(vault=vault, data={"model": "fake"})
    cfg.mb_dir.mkdir(parents=True, exist_ok=True)
    indexer.rebuild(cfg, progress=False)
    return cfg


PLANTED = [
    ("a.md", "The daemon is always started at session start, config daemon session index corpus."),
    ("b.md", "The daemon is never started at session start, config daemon session index corpus."),
]


def test_planted_polarity_pair_is_found(tmp_path, monkeypatch):
    cfg = _vault(tmp_path, monkeypatch, PLANTED)
    report = contradictions.scan(cfg, band=(0.5, 0.999))
    keys = {c["key"] for c in report["candidates"]}
    assert any("a.md" in k and "b.md" in k for k in keys)
    entry = next(c for c in report["candidates"] if "a.md" in c["key"])
    assert entry["evidence"]["polarity_flip"]


def test_planted_version_pair_is_found(tmp_path, monkeypatch):
    cfg = _vault(tmp_path, monkeypatch, [
        ("a.md", "The build requires python 3.11, config build index corpus rule."),
        ("b.md", "The build requires python 3.13, config build index corpus rule."),
    ])
    report = contradictions.scan(cfg, band=(0.5, 0.999))
    assert report["candidates"]
    assert "numeric_mismatch" in report["candidates"][0]["evidence"]


def test_exact_copies_are_not_candidates(tmp_path, monkeypatch):
    """Their home is curator.lint().duplicates — above the band ceiling."""
    body = "The daemon is always started, config daemon session index corpus rule."
    cfg = _vault(tmp_path, monkeypatch, [("a.md", body), ("b.md", body)])
    assert contradictions.scan(cfg)["candidates"] == []


def test_missing_index_reports_status(cfg):
    report = contradictions.scan(cfg)
    assert report["index_status"] == "missing" and report["candidates"] == []


def test_dismissed_pairs_disappear_from_the_next_run(tmp_path, monkeypatch):
    cfg = _vault(tmp_path, monkeypatch, PLANTED)
    first = contradictions.scan(cfg, band=(0.5, 0.999))
    assert first["candidates"]
    key = first["candidates"][0]["key"]

    contradictions.record_verdict(cfg, key, "false_positive")
    second = contradictions.scan(cfg, band=(0.5, 0.999))
    assert key not in {c["key"] for c in second["candidates"]}
    assert second["suppressed"] >= 1


def test_scan_is_deterministic(tmp_path, monkeypatch):
    cfg = _vault(tmp_path, monkeypatch, PLANTED)
    a = json.dumps(contradictions.scan(cfg, band=(0.5, 0.999)), sort_keys=True)
    b = json.dumps(contradictions.scan(cfg, band=(0.5, 0.999)), sort_keys=True)
    assert a == b


def test_limit_caps_the_report(tmp_path, monkeypatch):
    cfg = _vault(tmp_path, monkeypatch, PLANTED + [
        ("c.md", "The rule always applies, config daemon session index corpus."),
        ("d.md", "The rule never applies, config daemon session index corpus."),
    ])
    assert len(contradictions.scan(cfg, limit=1, band=(0.5, 0.999))["candidates"]) == 1


# --- CLI --------------------------------------------------------------------

def test_bare_curator_is_unchanged_by_the_new_subcommands(tmp_path, monkeypatch, capsys):
    """W07 acceptance: adding subcommands must not alter `mb curator` output."""
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    (tmp_path / "reference").mkdir(parents=True)
    (tmp_path / "reference" / "a.md").write_text(
        "---\ntype: reference\ncreated: 2026-01-01\ntitle: A\n---\n\n# A\n\n## S\nbody\n",
        encoding="utf-8")

    assert main(["curator", "--json"]) == 0
    first = capsys.readouterr().out
    assert main(["curator", "--json"]) == 0
    assert capsys.readouterr().out == first
    payload = json.loads(first)
    # The invariant is that a SUBCOMMAND cannot leak into the bare report — not
    # that the report may never grow. `stale_unused`/`usage_tracked_notes` are
    # findings of `mb curator` itself (2026-08-05), added additively so a
    # consumer reading known keys is unaffected; a key arriving here from
    # `contradictions` or `gaps` would still be the bug this guards.
    assert set(payload) == {"issues", "notes", "orphans", "stale",
                            "stale_unused", "usage_tracked_notes",
                            "link_suggestions", "lint"}, \
        "bare curator's JSON shape must not gain keys from the subcommands"


def test_json_flag_before_the_subcommand_is_not_swallowed(tmp_path, monkeypatch, capsys):
    """argparse gotcha: a subparser default would otherwise overwrite the
    parent's --json."""
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    assert main(["curator", "--json", "contradictions"]) == 0
    json.loads(capsys.readouterr().out)          # must be parseable JSON


def test_cli_contradictions_and_verdict(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    assert main(["curator", "contradictions", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["band"] == list(contradictions.CONTRADICTION_BAND)

    assert main(["curator", "verdict", "a#S||b#S", "--verdict", "resolved"]) == 0
    assert "verdict recorded" in capsys.readouterr().out
    cfg = Config(vault=tmp_path, data={})
    assert contradictions.read_journal(cfg)["verdicts"]["a#S||b#S"]["verdict"] == "resolved"


def test_human_output_is_honest_about_precision(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    assert main(["curator", "contradictions"]) == 0
    out = capsys.readouterr().out
    assert "CANDIDATES, not verdicts" in out
