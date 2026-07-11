"""AUD-22: connector contract + local reference connector + registry."""
import pytest

from singularity.config import Config
from singularity.connectors import validate_manifest
from singularity.connectors.base import Connector, NormalizedDoc
from singularity.connectors.local_files import LocalFilesConnector
from singularity.connectors import registry


@pytest.fixture
def docs_dir(tmp_path):
    (tmp_path / "a.md").write_text("# A\nalpha", encoding="utf-8")
    (tmp_path / "b.txt").write_text("beta", encoding="utf-8")
    (tmp_path / "skip.png").write_bytes(b"\x89PNG")
    return tmp_path


# --- contract ----------------------------------------------------------------

def test_local_connector_satisfies_protocol(docs_dir):
    c = LocalFilesConnector(docs_dir)
    assert isinstance(c, Connector)                 # runtime_checkable Protocol


def test_manifest_is_valid_and_declares_no_secret_values(docs_dir):
    m = LocalFilesConnector(docs_dir).manifest()
    assert validate_manifest(m) == []
    assert m.name == "local_files"
    assert "fetch" in m.capabilities
    assert m.secret_refs == []                      # references only, no values


def test_validate_manifest_flags_bad_manifest():
    from singularity.connectors.base import ConnectorManifest
    bad = ConnectorManifest(name="", secret_refs=[123], rate_limit_per_min=0)  # type: ignore
    errs = validate_manifest(bad)
    assert any("name" in e for e in errs)
    assert any("secret_refs" in e for e in errs)
    assert any("rate_limit" in e for e in errs)


# --- health + fetch + incremental cursor ------------------------------------

def test_health_reports_directory(docs_dir):
    assert LocalFilesConnector(docs_dir).health()["ok"] is True
    assert LocalFilesConnector(docs_dir / "nope").health()["ok"] is False


def test_fetch_returns_normalized_docs(docs_dir):
    docs, cursor = LocalFilesConnector(docs_dir).fetch()
    ids = {d.id for d in docs}
    assert ids == {"a.md", "b.txt"}                 # .png skipped
    assert all(isinstance(d, NormalizedDoc) and d.source == "local_files" for d in docs)
    assert cursor is not None


def test_fetch_is_incremental(docs_dir):
    c = LocalFilesConnector(docs_dir)
    _, cursor = c.fetch()
    docs2, _ = c.fetch(cursor)
    assert docs2 == []                              # nothing changed since cursor
    import os, time
    later = time.time() + 10
    os.utime(docs_dir / "a.md", (later, later))     # touch one file
    docs3, _ = c.fetch(cursor)
    assert {d.id for d in docs3} == {"a.md"}         # only the changed file


# --- registry: off by default -----------------------------------------------

def test_connectors_off_by_default(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    assert registry.enabled(cfg) == []
    with pytest.raises(PermissionError):
        registry.load(cfg, "local_files")


def test_enabled_connector_loads(tmp_path, docs_dir):
    cfg = Config(vault=tmp_path, data={
        "connectors": {"local_files": {"enabled": True, "root": str(docs_dir)}}})
    assert registry.enabled(cfg) == ["local_files"]
    conn = registry.load(cfg, "local_files")
    assert conn.health()["ok"] is True


def test_unknown_connector_raises(tmp_path):
    cfg = Config(vault=tmp_path, data={})
    with pytest.raises(KeyError):
        registry.load(cfg, "does_not_exist")


def test_available_lists_reference_connector():
    assert "local_files" in registry.available()
