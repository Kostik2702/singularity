"""Config surface of the code graph (CODE-GRAPH D1).

Two keys, both deliberate extensions of CODE-INDEX D1 recorded in the
decision: a separate on/off switch (the graph is a new, heavier store the
owner turns on; the semantic corpus stays governed by `code_repos_dir`), and
graph-only roots outside the repositories root, which never reach the
embedding corpus.
"""
import json

from singularity.config import DEFAULT_CONFIG, Config, validate_config


def test_the_graph_is_off_and_has_no_extra_roots_by_default():
    assert DEFAULT_CONFIG["code_graph_enabled"] is False
    assert DEFAULT_CONFIG["code_graph_extra_roots"] == []


def test_wrong_types_are_rejected_by_name():
    errors = validate_config({"code_graph_enabled": "yes",
                              "code_graph_extra_roots": "E:/work/mono"})["errors"]
    joined = " ".join(errors)
    assert "code_graph_enabled" in joined
    assert "code_graph_extra_roots" in joined


def test_the_store_is_machine_local(tmp_path):
    """Graph rows key absolute machine paths; AUD-19 keeps such state out of
    the portable `.mb` root, under the machine's own directory."""
    cfg = Config(vault=tmp_path, data={})
    assert cfg.code_graph_dir == cfg.machine_dir / "code_graph"


def test_extra_roots_expand_home_and_ignore_blanks(tmp_path):
    cfg = Config(vault=tmp_path, data={"code_graph_extra_roots": ["~/mono", "  ", str(tmp_path)]})
    roots = cfg.code_graph_extra_roots
    assert len(roots) == 2
    assert "~" not in str(roots[0])
    assert roots[1] == tmp_path


def test_the_shareable_example_never_carries_extra_roots(tmp_path):
    """The example is made to be committed and shared; an extra root is a
    path to someone's work repository and does not travel with it."""
    cfg = Config(vault=tmp_path, data={"code_graph_extra_roots": [str(tmp_path / "employer-mono")]})
    cfg.save()
    example = json.loads(cfg.config_example_path.read_text(encoding="utf-8"))
    assert "code_graph_extra_roots" not in example
