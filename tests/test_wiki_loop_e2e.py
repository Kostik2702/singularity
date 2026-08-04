"""W12: the ingest cycle end to end, and the data-safety regression that
decision D1 owes the vault.

Two things are proven here and nowhere else:

1. **The cycle closes.** A document goes in, a plan comes out with evidence,
   notes get written, the mark lands, and `mb curator gaps` agrees there is
   nothing left unprocessed — on a clean temporary vault, no manual steps.
2. **R8: `.mb/sources/` survives everything.** `mb uninstall`,
   `mb doctor --fix` and the documented rollback all run against a populated
   vault, and every raw file is byte-identical afterwards. This is the whole
   compensation for storing irreplaceable content under a directory the
   project documents as runtime state.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import shutil

import numpy as np
import pytest

from singularity import cli, gaps, ingest_plan, sources
from singularity.cli import main
from singularity.config import Config
from singularity.search import indexer


class FakeEncoder:
    """Token-overlap embedding: planted relationships must be reproducible,
    not left to a hash function."""

    model_name = "fake"
    VOCAB = ["индексатор", "корпус", "поиск", "эмбеддинг", "косинус", "vault",
             "демон", "правило", "всегда", "никогда", "0.45", "0.60", "порог"]

    def encode(self, texts, progress=False):
        out = np.zeros((len(texts), len(self.VOCAB) + 1), dtype=np.float32)
        for i, text in enumerate(texts):
            low = text.lower()
            vec = np.array([low.count(w) for w in self.VOCAB] + [1.0], dtype=np.float32)
            out[i] = vec / np.linalg.norm(vec)
        return out


SHARED = "индексатор корпус поиск эмбеддинг косинус vault "
SOURCE_TEXT = (
    "# Внешняя статья про поиск\n\n"
    "## Устройство корпуса\n" + SHARED * 5 + "\n\n"
    "## Порог отбора\n" + SHARED * 2 + "порог 0.60 всегда применяется. "
    "Ссылка на [[Совершенно Новая Тема]].\n"
)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    monkeypatch.setattr(indexer, "shared_encoder", lambda *_a, **_k: FakeEncoder())
    monkeypatch.setenv("MB_HOME", str(tmp_path))
    root = tmp_path
    bodies = {
        "reference/search.md": SHARED * 4,
        "reference/indexing.md": SHARED * 4,
        "howto/rebuild.md": SHARED * 4,
        # Planted contradiction against the source's "Порог отбора" section.
        # The shared-token mass is tuned so the pair lands INSIDE the (0.72,
        # 0.90) band: more overlap and curator.lint would own it as a
        # near-duplicate, less and it reads as a different topic.
        "rules/threshold.md": ("порог 0.45 никогда не меняется. "
                               "демон демон правило правило. " + SHARED * 2),
    }
    for rel, body in bodies.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\ntype: reference\ntitle: \"{p.stem}\"\ncreated: 2026-01-01\n"
                     f"---\n\n# {p.stem}\n\n## Раздел\n{body}",
                     encoding="utf-8")
    return root


def _cfg(vault):
    return Config(vault=vault, data={"model": "fake", "ingest_fanout_threshold": -1.0})


def _run(argv, capsys):
    code = main(argv)
    return code, capsys.readouterr()


def _hash_tree(root: pathlib.Path) -> dict[str, str]:
    if not root.exists():
        return {}
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


# --- the cycle --------------------------------------------------------------

def test_full_cycle_closes(vault, tmp_path, capsys, monkeypatch):
    from singularity.search import client
    monkeypatch.setattr(client, "ping", lambda cfg: None)      # cold: index explicitly
    cfg = _cfg(vault)

    doc = tmp_path / "article.md"
    doc.write_text(SOURCE_TEXT, encoding="utf-8")

    # 1. add
    code, out = _run(["ingest", "add", str(doc), "--title", "Внешняя статья", "--json"],
                     capsys)
    assert code == 0, out.err
    added = json.loads(out.out)
    sid = added["source_id"]
    assert added["indexed"] == "pending", "cold daemon must not block the add"
    assert (vault / added["stored_path"]).is_file()

    # 2. index both corpora
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")

    # 3. plan
    plan = ingest_plan.plan(cfg, sid)
    assert plan["error"] is None and plan["touch"], plan
    for entry in plan["touch"]:
        assert entry["why"]["source_section"] and entry["why"]["note_section"]
    assert plan["summary_target"]["exists"] is False
    assert any(p["name"] == "Совершенно Новая Тема" for p in plan["new_pages"])

    # 4. summary note with provenance
    code, out = _run(["note", "new", "reference", "Внешняя статья", "--from-source", sid],
                     capsys)
    assert code == 0, out.err
    summary = vault / "reference" / "Внешняя статья.md"
    assert f'sources: ["{sid}"]' in summary.read_text(encoding="utf-8")

    # 5. edit the pages the plan justified (add-only)
    touched = [entry["file"] for entry in plan["touch"][:3]]
    for rel in touched:
        path = vault / rel
        path.write_text(path.read_text(encoding="utf-8")
                        + f"\n\n## Из источника {sid}\n> цитата из внешней статьи\n",
                        encoding="utf-8")

    # 6. mark
    code, out = _run(["ingest", "mark", sid, "--notes",
                      ",".join(touched + ["reference/Внешняя статья.md"]),
                      "--status", "processed", "--actor", "skill", "--json"], capsys)
    assert code == 0, out.err
    assert json.loads(out.out)["status"] == "processed"

    # 7. the cycle is closed
    assert gaps.report(cfg)["counts"]["unprocessed_sources"] == 0

    # 8. marked pages stop resurfacing
    indexer.rebuild(cfg, progress=False)
    replan = ingest_plan.plan(cfg, sid)
    assert not set(touched) & {t["file"] for t in replan["touch"]}
    assert replan["suppressed"] >= len(touched)

    # 9. the journal is one parseable JSON object per line
    raw = sources.log_path(cfg).read_text(encoding="utf-8").strip().splitlines()
    assert len(raw) == 1
    row = json.loads(raw[0])
    assert row["op"] == "ingest" and row["source_id"] == sid and row["actor"] == "skill"


def test_planted_contradiction_is_surfaced(vault, tmp_path):
    """The wiki says the threshold is 0.45 and never changes; the source says
    0.60 and always applies."""
    cfg = _cfg(vault)
    sources.add(cfg, SOURCE_TEXT, origin_ref="ref")
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")

    sid = sources.list_sources(cfg)[0]["source_id"]
    plan = ingest_plan.plan(cfg, sid, threshold=-1.0)
    conflicts = plan["conflicts"]
    assert conflicts, "the planted disagreement must reach the plan"
    evidence = conflicts[0]["evidence"]
    assert {"numeric_mismatch", "polarity_flip"} & set(evidence)
    assert all(tokens for tokens in evidence.values()), "evidence is never empty"


def test_search_escalation_finds_only_in_sources(vault):
    cfg = _cfg(vault)
    sources.add(cfg, SOURCE_TEXT, origin_ref="ref")
    indexer.rebuild(cfg, progress=False)
    indexer.rebuild(cfg, progress=False, corpus="sources")

    from singularity.search.engine import SearchEngine
    engine = SearchEngine.__new__(SearchEngine)
    engine.cfg = cfg
    engine.encoder = FakeEncoder()
    engine.reload()
    cfg.data["min_score"] = -1.0

    plain = engine.search({"query": SHARED, "top_k": 20, "format": "json"})
    assert all(r["source"] != "sources" for r in plain["results"])
    escalated = engine.search({"query": SHARED, "top_k": 20, "format": "json",
                               "include_sources": True})
    assert any(r["source"] == "sources" for r in escalated["results"])


# --- R8: the data must survive every lifecycle operation --------------------

@pytest.fixture
def populated(vault, tmp_path, monkeypatch):
    cfg = _cfg(vault)
    sources.add(cfg, SOURCE_TEXT, origin_ref="a")
    sources.add(cfg, "# Второй\n\n## Раздел\n" + SHARED * 4, origin_ref="b")
    indexer.rebuild(cfg, progress=False, corpus="sources")
    return cfg


def test_uninstall_never_touches_raw_sources(populated, isolated_machine, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda vault_arg=None: populated)

    before = _hash_tree(populated.sources_dir)
    registry_before = sources.registry_path(populated).read_bytes()
    assert len(before) == 2

    assert cli.cmd_uninstall(populated, None) == 0

    assert _hash_tree(populated.sources_dir) == before
    assert sources.registry_path(populated).read_bytes() == registry_before


def test_doctor_fix_never_touches_raw_sources(populated, isolated_machine):
    # `--fix` registers OS scheduler jobs and rewrites client profiles. Left
    # unstubbed this test retargets the developer's actual nightly/watch tasks
    # at a pytest temp vault — observed once, repaired by hand, and now both
    # guarded (conftest) and neutralized (`isolated_machine`).
    before = _hash_tree(populated.sources_dir)
    registry_before = sources.registry_path(populated).read_bytes()

    from singularity import doctor
    doctor.run(populated, fix=True, heavy=False, as_json=True)

    assert _hash_tree(populated.sources_dir) == before
    assert sources.registry_path(populated).read_bytes() == registry_before


def test_documented_rollback_never_touches_raw_sources(populated):
    """The exact procedure in docs/ROLLBACK.md: turn the corpus off and delete
    the derived index. Raw content must be untouched by both steps."""
    before = _hash_tree(populated.sources_dir)
    registry_before = sources.registry_path(populated).read_bytes()

    populated.data["ingest_index_sources"] = False
    shutil.rmtree(populated.sources_index_dir)

    assert _hash_tree(populated.sources_dir) == before
    assert sources.registry_path(populated).read_bytes() == registry_before
    # and the layer still reports honestly rather than pretending to work
    plan = ingest_plan.plan(populated, sources.list_sources(populated)[0]["source_id"])
    assert "ingest_index_sources" in plan["error"]


def test_the_conftest_guard_actually_fires(populated):
    """The guard is the whole point of tests/conftest.py — prove it blocks a
    real registration instead of silently allowing it."""
    from singularity.platform import scheduler

    with pytest.raises(AssertionError, match="REAL OS scheduled task"):
        scheduler.register(scheduler.Job("nightly", [], daily_at="03:00"))
    with pytest.raises(AssertionError, match="REAL OS scheduled task"):
        scheduler.unregister(scheduler.Job("watch", [], at_login=True))


def test_index_rebuild_never_touches_raw_sources(populated):
    before = _hash_tree(populated.sources_dir)
    indexer.rebuild(populated, progress=False)
    indexer.rebuild(populated, progress=False, corpus="sources")
    assert _hash_tree(populated.sources_dir) == before


DELETE_PRIMITIVES = {"rmtree", "unlink", "remove", "rmdir", "move", "rename"}
# The ONE deletion the ingest layer performs: releasing its own mkdir lock.
DELETE_ALLOWLIST = {"lock"}


def test_no_wiki_loop_module_can_delete_a_source():
    """Static backstop for R8: nothing in the ingest layer deletes or moves a
    path other than its own lock directory.

    Checked over the AST rather than by substring, so `shutil.rmtree(lock)`
    (correct) and `shutil.rmtree(cfg.sources_dir)` (catastrophic) are told
    apart by their ARGUMENT instead of both matching the word 'rmtree'."""
    import ast

    package = pathlib.Path(cli.__file__).resolve().parent
    offenders: list[str] = []
    for name in ("sources.py", "ingest_plan.py", "contradictions.py", "gaps.py"):
        tree = ast.parse((package / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else \
                (func.id if isinstance(func, ast.Name) else None)
            if attr not in DELETE_PRIMITIVES:
                continue
            target = node.args[0] if node.args else func.value if \
                isinstance(func, ast.Attribute) else None
            rendered = ast.unparse(target) if target is not None else "?"
            if rendered.split(".")[0] not in DELETE_ALLOWLIST:
                offenders.append(f"{name}:{node.lineno} {attr}({rendered})")
    assert offenders == [], f"deletion outside the allowlist: {offenders}"
