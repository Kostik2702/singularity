"""AUD-14: secret storage hardening. Tests never touch the real keyring or the
real fallback file — _keyring is stubbed and _FALLBACK is redirected to tmp.
"""
import json
import os

import pytest

from singularity.platform import secrets


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def get_password(self, service, name):
        return self.store.get((service, name))

    def delete_password(self, service, name):
        del self.store[(service, name)]


@pytest.fixture
def fallback(tmp_path, monkeypatch):
    p = tmp_path / "secrets.json"
    monkeypatch.setattr(secrets, "_FALLBACK", p)
    return p


def _no_keyring(monkeypatch):
    monkeypatch.setattr(secrets, "_keyring", lambda: None)


def _fake_keyring(monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(secrets, "_keyring", lambda: kr)
    return kr


# --- keyring path ------------------------------------------------------------

def test_keyring_roundtrip(monkeypatch, fallback):
    _fake_keyring(monkeypatch)
    assert secrets.set_secret("token", "s3cr3t") == "keyring"
    assert secrets.get_secret("token") == "s3cr3t"
    assert not fallback.exists()          # nothing written in plaintext


def test_keyring_delete_and_list(monkeypatch, fallback):
    _fake_keyring(monkeypatch)
    secrets.set_secret("a", "1")
    secrets.set_secret("b", "2")
    assert set(secrets.list_secrets()) == {"a", "b"}
    assert secrets.delete_secret("a") is True
    assert "a" not in secrets.list_secrets()


# --- fallback requires explicit opt-in --------------------------------------

def test_fallback_refused_without_opt_in(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    with pytest.raises(secrets.SecretsError):
        secrets.set_secret("token", "s3cr3t")
    assert not fallback.exists()          # nothing leaked to disk


def test_fallback_used_with_opt_in(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    assert secrets.set_secret("token", "s3cr3t", allow_plaintext=True) == "file-fallback"
    assert secrets.get_secret("token") == "s3cr3t"


def test_fallback_is_0600_on_posix(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    secrets.set_secret("token", "s3cr3t", allow_plaintext=True)
    if os.name != "nt":
        mode = fallback.stat().st_mode & 0o777
        assert mode == 0o600
    assert not list(fallback.parent.glob("*.tmp"))   # atomic: no temp left


def test_fallback_list_and_delete(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    secrets.set_secret("a", "1", allow_plaintext=True)
    secrets.set_secret("b", "2", allow_plaintext=True)
    assert set(secrets.list_secrets()) == {"a", "b"}
    assert secrets.delete_secret("a") is True
    assert secrets.get_secret("a") is None


# --- corruption recovery -----------------------------------------------------

def test_corrupt_fallback_reads_as_none(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    fallback.write_text("{not json", encoding="utf-8")
    assert secrets.get_secret("token") is None    # no crash


def test_corrupt_fallback_backed_up_on_write(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    fallback.write_text("{not json", encoding="utf-8")
    secrets.set_secret("token", "s3cr3t", allow_plaintext=True)
    assert secrets.get_secret("token") == "s3cr3t"
    assert list(fallback.parent.glob(f"{fallback.name}.corrupt-*"))


# --- values never exposed via list ------------------------------------------

def test_list_never_returns_values(monkeypatch, fallback):
    _no_keyring(monkeypatch)
    secrets.set_secret("token", "VERYSECRET", allow_plaintext=True)
    assert "VERYSECRET" not in json.dumps(secrets.list_secrets())
