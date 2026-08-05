"""Gate for capture-time secret redaction.

Two failure modes, and both are fatal in opposite directions:

* A secret that gets through lands in git history, on every machine the vault
  reaches. Unfixable after the fact.
* A false positive on ordinary prose makes the check annoying, and an annoying
  security check gets switched off. Then it protects nothing.

So the positives and the negatives carry equal weight here.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from singularity.knowledge.redact import MASK, format_notice, redact  # noqa: E402


# --- the secret must not survive -------------------------------------------

@pytest.mark.parametrize("kind,sample,marker", [
    ("aws-access-key", "creds are AKIAIOSFODNN7EXAMPLE in the profile", "AKIA"),
    ("openai-key", "export OPENAI=sk-abcdefghijklmnopqrstuvwxyz0123456789", "sk-"),
    ("anthropic-key", "key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUv-wXyZ012345", "sk-ant-"),
    ("github-token", "use ghp_16CharsAndThenSomeMore0123456789abcdef now", "ghp_"),
    ("slack-token", "bot token xoxb-1234567890-ABCdefGHIjklMNO", "xoxb-"),
    # exactly 35 chars after the AIza marker — a real Google key is 39 total,
    # and the rule is length-anchored on purpose so `AIza`-prefixed prose misses
    ("google-api-key", "maps AIzaSyD_1234567890abcdefghijklmnopqrstu key", "AIza"),
    ("bearer-token", "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9", "Bearer"),
])
def test_known_shapes_are_masked_and_reported(kind, sample, marker):
    out = redact(sample)
    assert out.changed, f"{kind} was not detected"
    assert MASK in out.text
    assert {f.kind for f in out.findings} == {kind}
    # the type marker survives so a later reader knows WHAT was here...
    assert marker in out.text
    # ...but nothing else from the original value does.
    secret_tail = sample.split(marker, 1)[1].split()[0]
    assert secret_tail not in out.text


def test_url_credentials_lose_only_the_password():
    out = redact("connect to postgres://svc_user:h4nT3r2Secret@db.internal:5432/app")
    assert out.changed
    assert "h4nT3r2Secret" not in out.text
    assert "svc_user" in out.text and "db.internal" in out.text


def test_private_key_block_body_is_gone():
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEAx7Ku9bV0aQ1oNnQ8\n"
           "Zt5cWpLmR3sYtKfQ2vHgD1eXbNjA0pLs\n"
           "-----END RSA PRIVATE KEY-----")
    out = redact(f"the deploy key is:\n{pem}\nand it rotates monthly")
    assert out.changed
    assert "MIIEowIBAAKCAQEAx7Ku9bV0aQ1oNnQ8" not in out.text
    assert "Zt5cWpLmR3sYtKfQ2vHgD1eXbNjA0pLs" not in out.text
    assert "rotates monthly" in out.text


def test_generic_assignment_with_entropy_is_masked():
    out = redact('config had api_key="Xk29fLmQ8vTz01Bw" and it broke on reload')
    assert out.changed
    assert "Xk29fLmQ8vTz01Bw" not in out.text
    assert [f.kind for f in out.findings] == ["assigned-secret"]


# --- ordinary prose must survive untouched ---------------------------------

@pytest.mark.parametrize("text", [
    "the password was rotated after the incident",
    "set api_key in config before the first run",
    "token: <your-token-here> goes in the header",
    "password=${VAULT_PASSWORD} is read from the environment",
    "api_key: config/keys/service.json holds the path, not the value",
    "secret: XXXXXXXX was already redacted upstream",
    "auth_token = changeme",
    "the client_secret must never be committed",
    "IndexLockedError is raised when a second writer arrives",
    "sk- prefixes belong to OpenAI, gh_ to GitHub",
])
def test_prose_is_not_touched(text):
    out = redact(text)
    assert not out.changed, f"false positive on: {text!r} -> {out.findings}"
    assert out.text == text


def test_teaching_line_is_left_alone():
    """`mb learn` about how secrets are configured is documentation. Masking the
    example would corrupt the very note that explains the format."""
    text = 'for example api_key=Ab12Cd34Ef56Gh78 is the shape the header expects'
    out = redact(text)
    assert not out.changed
    assert out.text == text


# --- the report must not become the leak -----------------------------------

def test_findings_never_carry_the_value():
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    out = redact(f"key {secret}")
    blob = " ".join(f"{f.kind}|{f.hint}" for f in out.findings) + " ".join(format_notice(out.findings))
    assert "abcdefghijklmnopqrstuvwxyz" not in blob


def test_line_numbers_survive_a_collapsing_block():
    """The private-key rule removes lines. If findings were computed on the
    rewritten text, everything after the block would point at the wrong line —
    a security notice sending you to the wrong place. Planting the block above
    a second secret is what proves the two-pass design."""
    pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
           "MIIEowIBAAKCAQEAx7Ku9bV0aQ1oNnQ8\n"
           "Zt5cWpLmR3sYtKfQ2vHgD1eXbNjA0pLs\n"
           "AbCdEfGhIjKlMnOpQrStUvWxYz012345\n"
           "-----END RSA PRIVATE KEY-----")
    text = f"line one\n{pem}\nAKIAIOSFODNN7EXAMPLE trailing"
    out = redact(text)
    aws = [f for f in out.findings if f.kind == "aws-access-key"]
    assert len(aws) == 1
    # 1 "line one" + 5 PEM lines -> the AWS key is on line 7 of the ORIGINAL
    assert aws[0].line == 7, f"expected original line 7, got {aws[0].line}"


def test_clean_text_returns_the_same_object_contentwise():
    text = "nothing sensitive here at all"
    out = redact(text)
    assert out.text == text and out.findings == [] and not out.changed


def test_empty_input_is_safe():
    assert redact("").text == ""
    assert redact("").findings == []


def test_notice_is_summarised_not_enumerated_forever():
    text = "\n".join(f"key{i} sk-abcdefghijklmnopqrstuvwxyz012345{i:02d}" for i in range(12))
    out = redact(text)
    assert len(out.findings) == 12
    lines = format_notice(out.findings)
    assert lines[0].startswith("redacted 12 secret-shaped value(s)")
    assert any("and 7 more" in ln for ln in lines)
