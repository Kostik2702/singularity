"""The inbox files nothing on its own (AUD-21).

The whole value of a staging area is the guarantee that a capture stays put
until a person decides. So the tests here are mostly about what does NOT
happen: suggest must not move, classify or write; capture must not look like a
filed note; approve must refuse rather than guess.
"""
from __future__ import annotations

import pytest

from singularity import cli
from singularity.config import Config
from singularity.knowledge import inbox


@pytest.fixture()
def cfg(tmp_path) -> Config:
    (tmp_path / ".mb").mkdir(parents=True)
    return Config(vault=tmp_path, data={"model": "fake", "inbox_enabled": True})


def test_capture_writes_verbatim_and_is_not_a_managed_note(cfg):
    out = inbox.capture(cfg, "Демон не перечитывает конфиг после set")
    text = cfg.resolve(out["file"]).read_text(encoding="utf-8")
    assert "Демон не перечитывает конфиг после set" in text
    # `type: inbox` is not a managed type, so curator/digest/search filters
    # cannot mistake a capture for knowledge that was actually filed.
    assert "type: inbox" in text
    assert out["file"].startswith("inbox/")


def test_two_captures_with_the_same_first_line_do_not_overwrite(cfg):
    a = inbox.capture(cfg, "same opening line\n\nbody one")
    b = inbox.capture(cfg, "same opening line\n\nbody two")
    assert a["file"] != b["file"]
    assert len(inbox.items(cfg)) == 2


def test_list_is_oldest_first(cfg):
    inbox.capture(cfg, "first")
    inbox.capture(cfg, "second")
    titles = [i["title"] for i in inbox.items(cfg)]
    assert titles == ["first", "second"]


def test_suggest_proposes_and_changes_nothing(cfg):
    captured = inbox.capture(cfg, "Мы решили: конфиг уезжает из vault. trade-off принят.")
    before = sorted(p.name for p in inbox.inbox_dir(cfg).iterdir())

    def fake_search(_req):
        return {"results": [
            {"meta": {"file": "decisions/config.md", "title": "Config"}, "score": 0.91},
            {"meta": {"file": "howto/setup.md", "title": "Setup"}, "score": 0.40},
        ]}

    out = inbox.suggest(cfg, captured["file"], search=fake_search)
    assert out["suggested_type"] == "decision"
    assert [d["file"] for d in out["duplicates"]] == ["decisions/config.md"]
    assert [ln["file"] for ln in out["links"]] == ["howto/setup.md"]
    assert sorted(p.name for p in inbox.inbox_dir(cfg).iterdir()) == before
    assert not (cfg.vault / "decisions").exists(), "suggest must not create anything"


def test_suggest_without_a_search_still_answers(cfg):
    """A daemon round-trip is the caller's choice, not a precondition."""
    captured = inbox.capture(cfg, "Всегда снимай exit-код с самой команды")
    out = inbox.suggest(cfg, captured["file"])
    assert out["suggested_type"] == "rule"
    assert out["duplicates"] == [] and out["links"] == []


def test_approve_files_into_the_managed_root_and_keeps_provenance(cfg):
    captured = inbox.capture(cfg, "Как поднять демон\n\n1. mb daemon start")
    out = inbox.approve(cfg, captured["file"], "howto")
    assert out["file"].startswith("howto/")
    filed = cfg.resolve(out["file"]).read_text(encoding="utf-8")
    assert "type: howto" in filed
    assert "1. mb daemon start" in filed
    # when it was thought, not when it was filed
    assert f"captured: {captured['captured']}" in filed
    assert not cfg.resolve(captured["file"]).exists(), "the capture must leave the inbox"


def test_approve_refuses_an_unknown_type(cfg):
    captured = inbox.capture(cfg, "text")
    out = inbox.approve(cfg, captured["file"], "nonsense")
    assert "unknown type" in out["error"]
    assert cfg.resolve(captured["file"]).exists()


def test_approve_never_overwrites_an_existing_note(cfg):
    (cfg.vault / "howto").mkdir(parents=True)
    (cfg.vault / "howto" / "Taken.md").write_text("existing\n", encoding="utf-8")
    captured = inbox.capture(cfg, "Taken\n\nbody")
    out = inbox.approve(cfg, captured["file"], "howto")
    assert "already exists" in out["error"]
    assert (cfg.vault / "howto" / "Taken.md").read_text(encoding="utf-8") == "existing\n"
    assert cfg.resolve(captured["file"]).exists()


def test_guess_type_is_a_hint_with_a_safe_default():
    assert inbox.guess_type("мы решили не делить пакет") == "decision"
    assert inbox.guess_type("сломалось после релиза, traceback") == "incident"
    assert inbox.guess_type("просто наблюдение без сигналов") == "learning"


def test_the_command_is_off_until_enabled(tmp_path, capsys):
    (tmp_path / ".mb").mkdir(parents=True)
    assert cli.main(["--vault", str(tmp_path), "inbox", "list"]) == 2
    assert "inbox is off" in capsys.readouterr().err
