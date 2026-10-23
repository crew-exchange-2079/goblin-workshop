import os

import pytest

from conftest import NOW, POLICY, fixture_files
from dagu_lint import secrets

# Provider-format tokens are assembled at runtime so that this repository
# never contains a string that secret scanners would treat as a live key.
SYNTHETIC = {
    "GitHub token": "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "GitLab token": "glp" + "at-" + "x1Y2z3A4b5C6d7E8f9G0",
    "Slack token": "xo" + "xb-" + "1234567890-abcdefghij",
    "Slack webhook URL": "https://hooks.slack.com/services/" + "T0000/B0000/" + "XXXXXXXXXXXXXXXX",
    "Stripe live key": "sk" + "_live_" + "abcdefghijklmnop1234",
    "Google API key": "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
}


@pytest.mark.parametrize("label,value", sorted(SYNTHETIC.items()))
def test_known_patterns(label, value):
    assert secrets.known_pattern(f"prefix {value} suffix") == label


def test_url_with_reference_password_is_not_flagged():
    assert secrets.known_pattern("postgres://app:${DB_PASSWORD}@db/app") is None
    assert secrets.known_pattern("postgres://app:$DB_PASSWORD@db/app") is None
    assert secrets.known_pattern("https://example.invalid/path") is None


@pytest.mark.parametrize("value", ["${TOKEN}", "$TOKEN", "`cat /run/secrets/token`", "$(vault read -field=x a)"])
def test_references(value):
    assert secrets.is_reference(value)


@pytest.mark.parametrize("value", ["hunter2", "${A}${B}", "prefix-${A}"])
def test_not_pure_references(value):
    assert not secrets.is_reference(value)


@pytest.mark.parametrize("key", ["DB_PASSWORD", "password", "API_KEY", "apiKey", "GITHUB_TOKEN", "client_secret",
                                 "AWS_SECRET_ACCESS_KEY", "smtp.password", "PG_PASSWORD"])
def test_secret_keys(key):
    assert secrets.secret_key(key)


@pytest.mark.parametrize("key", ["LOG_LEVEL", "TOKENIZER_MODEL", "passthrough", "author", "KEYBOARD"])
def test_non_secret_keys(key):
    assert not secrets.secret_key(key)


def test_entropy_bounds():
    assert secrets.entropy("") == 0
    assert secrets.entropy("aaaa") == 0
    assert secrets.entropy("abcd") == 2.0


@pytest.mark.parametrize("value", [
    "registry.example.invalid/app@sha256:" + "9b2a5c1e7f3d4a6b" * 4,   # hex digest
    "3f2504e0-4f89-11d3-9a0c-0305e82c3301",                             # UUID
    "/usr/local/bin/some_long_program_name_here",                       # path
    "ThisIsJustAVeryLongCamelCaseIdentifierName",                       # no digits
])
def test_entropy_ignores_common_non_secrets(value):
    assert secrets.high_entropy_tokens(value, 20, 4.0, []) == []


def test_entropy_flags_random_token():
    hits = secrets.high_entropy_tokens("Bearer Zx9Qk2LmP4rT7vW1yB3nD6fH8jK0sAa", 20, 4.0, [])
    assert hits and hits[0][0] == 31


def test_entropy_allow_patterns():
    import re

    assert secrets.high_entropy_tokens("Zx9Qk2LmP4rT7vW1yB3nD6fH8jK0sAa", 20, 4.0, [re.compile("^Zx9")]) == []


SECRET_FIXTURES = [p for p in fixture_files("bad") if os.path.basename(p).startswith("secret-")]


def _secret_values(path):
    # Every literal value in the fixture that a finding is about.
    from dagu_lint.loader import load_documents
    from dagu_lint.rules import _scalars

    with open(path, encoding="utf-8") as fh:
        doc = load_documents(fh.read())[0]
    vals = []
    for key, value, *_ in _scalars(doc, ""):
        if isinstance(value, str) and (secrets.known_pattern(value) or (key and secrets.secret_key(str(key)))
                                       or secrets.high_entropy_tokens(value, 20, 4.0, [])):
            vals.append(value)
    return vals


@pytest.mark.parametrize("path", SECRET_FIXTURES, ids=os.path.basename)
@pytest.mark.parametrize("fmt", ["text", "json", "github"])
def test_secret_values_are_never_printed(path, fmt, run_cli):
    values = _secret_values(path)
    assert values
    _, out, err = run_cli("-c", POLICY, "--format", fmt, "--now", NOW, path)
    for v in values:
        for chunk in (v, v.strip()[:12]):
            assert chunk not in out and chunk not in err
