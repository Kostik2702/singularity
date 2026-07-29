"""SUI-005: the sanctioned, versioned, read-only graph export.

Contract: [[Singularity graph export contract v1]] (vault reference note,
written before this test, per SDD).

Why this exists at all: Singularity UI needs the vault's resolved link graph,
and the only thing that currently produces it is `curator.link_graph()` — a
private function returning Python objects. An external Rust host cannot call
it, and pinning a consumer to a private signature turns every refactor into a
cross-project break. So the graph gets a real contract, and this file is that
contract's enforcement.

Every test here maps to a rule in §3/§5/§6 of the contract note.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from singularity import graph_export
from singularity.config import Config


@pytest.fixture
def cfg(tmp_path):
    return Config(vault=tmp_path, data={})


def _note(vault, rel: str, body: str = "", **frontmatter) -> None:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}: [{', '.join(value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# §3 Schema
# ---------------------------------------------------------------------------

def test_snapshot_has_the_contract_envelope(cfg):
    _note(cfg.vault, "reference/A.md", "no links", type="reference", status="active")

    snap = graph_export.build_snapshot(cfg)

    assert snap["schemaVersion"] == 1
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", snap["snapshotId"])
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", snap["generatedAt"])
    assert snap["counts"] == {"nodes": 1, "edges": 0, "brokenLinks": 0}
    assert isinstance(snap["nodes"], list) and isinstance(snap["edges"], list)
    assert snap["diagnostics"]["brokenLinks"] == []


def test_node_carries_the_declared_fields_and_never_invents_values(cfg):
    _note(cfg.vault, "reference/A.md", "# Title A\nbody",
          type="reference", status="active", project="proj", tags=["x", "y"])
    _note(cfg.vault, "reference/B.md", "# Bare\nbody")  # no type/status/project

    nodes = {n["id"]: n for n in graph_export.build_snapshot(cfg)["nodes"]}

    a = nodes["reference/A.md"]
    assert a["type"] == "reference"
    assert a["status"] == "active"
    assert a["project"] == "proj"
    assert a["tags"] == ["x", "y"]
    assert a["aliases"] == []

    b = nodes["reference/B.md"]
    # Absent frontmatter is null, not "unknown", not "" and not omitted.
    assert b["type"] is None and b["status"] is None and b["project"] is None
    assert b["tags"] == [] and b["aliases"] == []
    assert b["title"] == "Bare"  # first heading wins when frontmatter has no title


def test_edges_are_resolved_deduplicated_and_directed(cfg):
    _note(cfg.vault, "reference/A.md", "[[B]] and again [[B]] and [[A]]")
    _note(cfg.vault, "reference/B.md", "no outlinks")

    snap = graph_export.build_snapshot(cfg)

    assert snap["edges"] == [
        {"source": "reference/A.md", "target": "reference/B.md", "kind": "wikilink"}
    ]
    # Self-links are dropped, duplicates collapse to one edge.
    assert snap["counts"]["edges"] == 1


def test_every_edge_endpoint_exists_in_nodes(cfg):
    _note(cfg.vault, "reference/A.md", "[[B]]")
    _note(cfg.vault, "reference/B.md", "[[A]]")

    snap = graph_export.build_snapshot(cfg)
    ids = {n["id"] for n in snap["nodes"]}
    for edge in snap["edges"]:
        assert edge["source"] in ids and edge["target"] in ids


def test_broken_links_are_diagnostics_not_nodes(cfg):
    _note(cfg.vault, "reference/A.md", "[[Nowhere At All]]")

    snap = graph_export.build_snapshot(cfg, include_broken_links=True)

    assert [n["id"] for n in snap["nodes"]] == ["reference/A.md"]
    assert snap["diagnostics"]["brokenLinks"] == [
        {"source": "reference/A.md", "target": "Nowhere At All"}
    ]
    assert snap["counts"]["brokenLinks"] == 1


def test_broken_links_are_counted_even_when_the_list_is_omitted(cfg):
    _note(cfg.vault, "reference/A.md", "[[Nowhere At All]]")

    snap = graph_export.build_snapshot(cfg, include_broken_links=False)

    # The count is cheap and tells a consumer whether asking for the list is
    # worth it; suppressing the list must not silently zero the count.
    assert snap["counts"]["brokenLinks"] == 1
    assert snap["diagnostics"]["brokenLinks"] == []


def test_links_resolve_through_aliases_and_titles(cfg):
    _note(cfg.vault, "reference/A.md", "[[The Alias]] [[Real Title]]")
    _note(cfg.vault, "reference/B.md", "b", title="Real Title", aliases=["The Alias"])

    snap = graph_export.build_snapshot(cfg)

    assert snap["counts"]["edges"] == 1  # both link forms resolve to the same node
    assert snap["edges"][0]["target"] == "reference/B.md"


def test_ordering_is_deterministic(cfg):
    for name in ("C", "A", "B"):
        _note(cfg.vault, f"reference/{name}.md", "[[A]] [[B]] [[C]]")

    snap = graph_export.build_snapshot(cfg)

    assert [n["id"] for n in snap["nodes"]] == sorted(n["id"] for n in snap["nodes"])
    keys = [(e["source"], e["target"], e["kind"]) for e in snap["edges"]]
    assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# §3 snapshotId semantics
# ---------------------------------------------------------------------------

def test_snapshot_id_is_content_addressed_and_ignores_generation_time(cfg):
    _note(cfg.vault, "reference/A.md", "[[B]]")
    _note(cfg.vault, "reference/B.md", "b")

    first = graph_export.build_snapshot(cfg)
    second = graph_export.build_snapshot(cfg)

    assert first["snapshotId"] == second["snapshotId"]
    # Same content, same id, even though generatedAt may differ between runs.
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]


def test_snapshot_id_changes_when_content_changes(cfg):
    _note(cfg.vault, "reference/A.md", "no links")
    before = graph_export.build_snapshot(cfg)["snapshotId"]

    _note(cfg.vault, "reference/B.md", "[[A]]")
    after = graph_export.build_snapshot(cfg)["snapshotId"]

    assert before != after


# ---------------------------------------------------------------------------
# §3 Exclusions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("excluded", ["_moc/index.md", "changelog/2026-07.md",
                                      "daily/2026-07-29.md", "templates/note.md"])
def test_excluded_prefixes_appear_nowhere(cfg, excluded):
    _note(cfg.vault, excluded, "[[A]]")
    _note(cfg.vault, "reference/A.md", "a")

    snap = graph_export.build_snapshot(cfg)

    assert [n["id"] for n in snap["nodes"]] == ["reference/A.md"]
    assert snap["edges"] == []


# ---------------------------------------------------------------------------
# §5 Cross-platform
# ---------------------------------------------------------------------------

def test_ids_are_posix_relative_paths_on_every_platform(cfg):
    _note(cfg.vault, "projects/deep/nested/A.md", "a")

    snap = graph_export.build_snapshot(cfg)

    assert snap["nodes"][0]["id"] == "projects/deep/nested/A.md"
    assert "\\" not in snap["nodes"][0]["id"]


def test_snapshot_id_is_insensitive_to_line_endings(cfg):
    (cfg.vault / "reference").mkdir(parents=True)
    (cfg.vault / "reference" / "A.md").write_bytes(b"---\ntype: reference\n---\n\n# A\n[[B]]\n")
    (cfg.vault / "reference" / "B.md").write_bytes(b"---\ntype: reference\n---\n\n# B\n")
    lf = graph_export.build_snapshot(cfg)["snapshotId"]

    (cfg.vault / "reference" / "A.md").write_bytes(b"---\r\ntype: reference\r\n---\r\n\r\n# A\r\n[[B]]\r\n")
    (cfg.vault / "reference" / "B.md").write_bytes(b"---\r\ntype: reference\r\n---\r\n\r\n# B\r\n")
    crlf = graph_export.build_snapshot(cfg)["snapshotId"]

    # A Windows checkout and a Linux checkout of identical content must agree,
    # or the UI's snapshot cache thrashes on every platform switch.
    assert lf == crlf


def test_vault_path_with_spaces_and_non_ascii(tmp_path):
    vault = tmp_path / "memory brain память"
    vault.mkdir()
    cfg = Config(vault=vault, data={})
    _note(vault, "reference/Заметка.md", "[[Другая]]")
    _note(vault, "reference/Другая.md", "x")

    snap = graph_export.build_snapshot(cfg)

    assert snap["counts"]["nodes"] == 2
    assert snap["counts"]["edges"] == 1


# ---------------------------------------------------------------------------
# §6 Error behaviour
# ---------------------------------------------------------------------------

def test_one_unreadable_note_does_not_deny_the_whole_graph(cfg, monkeypatch):
    _note(cfg.vault, "reference/A.md", "[[B]]")
    _note(cfg.vault, "reference/B.md", "b")

    real_read = graph_export.Path.read_text

    def explode(self, *a, **k):
        if self.name == "B.md":
            raise OSError("simulated unreadable note")
        return real_read(self, *a, **k)

    monkeypatch.setattr(graph_export.Path, "read_text", explode)

    snap = graph_export.build_snapshot(cfg)

    # A is still exported; B is recorded as skipped rather than silently lost.
    assert "reference/A.md" in {n["id"] for n in snap["nodes"]}
    assert snap["diagnostics"]["unreadable"]


# ---------------------------------------------------------------------------
# CLI surface (§2)
# ---------------------------------------------------------------------------

def test_cli_graph_export_prints_json(cfg, capsys, monkeypatch):
    from singularity import cli
    _note(cfg.vault, "reference/A.md", "[[B]]")
    _note(cfg.vault, "reference/B.md", "b")
    monkeypatch.setattr(cli, "load_config", lambda _vault: cfg)

    rc = cli.main(["graph", "export", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schemaVersion"] == 1
    assert payload["counts"]["edges"] == 1


def test_cli_graph_export_writes_file_atomically(cfg, tmp_path, monkeypatch):
    from singularity import cli
    _note(cfg.vault, "reference/A.md", "a")
    monkeypatch.setattr(cli, "load_config", lambda _vault: cfg)
    out = tmp_path / "out" / "graph.json"

    rc = cli.main(["graph", "export", "--out", str(out)])

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["counts"]["nodes"] == 1
    # No temp leftovers next to the target.
    assert [p.name for p in out.parent.iterdir()] == ["graph.json"]


def test_committed_fixture_still_matches_the_live_export():
    """Drift gate for the external consumer.

    `tests/fixtures/graph_snapshot_v1.json` is the byte-for-byte sample shipped
    to Singularity UI as `contracts/memory/graph-snapshot-v1.example.json`. If
    this fails, the export changed and the consumer's fixture is now a lie --
    regenerate BOTH copies and decide whether the change needs a schemaVersion
    bump per §4 of the contract.
    """
    fixtures = Path(__file__).parent / "fixtures"
    cfg = Config(vault=fixtures / "graph_vault", data={})

    live = graph_export.build_snapshot(cfg, include_broken_links=True)
    committed = json.loads((fixtures / "graph_snapshot_v1.json").read_text(encoding="utf-8"))

    # generatedAt is frozen in the fixture on purpose: it is a contract sample,
    # not a run log, and it is excluded from snapshotId for the same reason.
    live.pop("generatedAt")
    committed.pop("generatedAt")
    assert live == committed


def test_fixture_vault_exercises_every_contract_rule():
    """The sample is only useful if it actually covers the awkward cases."""
    fixtures = Path(__file__).parent / "fixtures"
    snap = json.loads((fixtures / "graph_snapshot_v1.json").read_text(encoding="utf-8"))

    ids = [n["id"] for n in snap["nodes"]]
    assert not any(i.startswith("daily/") for i in ids)            # exclusion
    assert any(n["type"] is None for n in snap["nodes"])           # bare note
    assert any(n["aliases"] for n in snap["nodes"])                # alias resolution
    assert any(not any(e["source"] == n["id"] or e["target"] == n["id"]
                       for e in snap["edges"]) for n in snap["nodes"])  # orphan
    assert snap["diagnostics"]["brokenLinks"]                      # broken link
    assert any(ord(c) > 127 for n in snap["nodes"] for c in n["title"])  # unicode
    # A note linking to itself and links inside code fences produced no edges.
    assert all(e["source"] != e["target"] for e in snap["edges"])
    assert not any("Inline Code Link" in b["target"] for b in snap["diagnostics"]["brokenLinks"])


def test_cli_graph_export_never_writes_to_the_vault(cfg, monkeypatch, capsys):
    from singularity import cli
    _note(cfg.vault, "reference/A.md", "a")
    monkeypatch.setattr(cli, "load_config", lambda _vault: cfg)
    before = {p.relative_to(cfg.vault).as_posix() for p in cfg.vault.rglob("*")}

    cli.main(["graph", "export", "--json"])
    capsys.readouterr()

    after = {p.relative_to(cfg.vault).as_posix() for p in cfg.vault.rglob("*")}
    assert before == after
