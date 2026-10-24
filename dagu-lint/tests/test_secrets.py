import json
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


LEAKY = os.path.join(os.path.dirname(SECRET_FIXTURES[0]), "secret-in-malformed-schedule.yaml")


@pytest.mark.parametrize("fmt", ["text", "json", "github"])
@pytest.mark.parametrize("extra", [[], ["--disable", "DL402"], ["--disable", "DL402", "--disable", "DL201"]])
def test_echoing_diagnostics_are_redacted(fmt, extra, run_cli):
    # DL201 quotes the schedule it failed to parse; the password must not
    # survive into any output, even with the secret rule turned off.
    _, out, err = run_cli("-c", POLICY, "--format", fmt, "--now", NOW, *extra, LEAKY)
    assert "Sup3rS3cretPw" not in out + err
    if "DL201" not in extra:
        assert "DL201" in out and secrets.REDACTED in out


def test_queue_name_echo_is_redacted(tmp_path, run_cli):
    p = tmp_path / "q.yaml"
    p.write_text('name: x\nqueue: "postgres://u:Pa55w0rdHere@h/db"\nsteps:\n  - run: echo\n')
    _, out, _ = run_cli("-q", "default", "--format", "json", str(p))
    assert "Pa55w0rdHere" not in out and "DL101" in out


def test_redact():
    token = "Zx9Qk2LmP4rT7vW1yB3nD6fH8jK0sAa"
    assert secrets.redact(f"bad value {token!r}") == "bad value '<redacted>'"
    assert secrets.redact("db: postgres://u:hunter22@h/x") == "db: <redacted>h/x"
    assert secrets.redact("flagged hunter2 here", {"hunter2"}) == "flagged <redacted> here"
    assert secrets.redact("short abc", {"abc"}) == "short abc"  # too short to scrub safely
    assert secrets.redact("minute: 'postgres:' is not a number") == "minute: 'postgres:' is not a number"


@pytest.mark.parametrize("value", ["1234567890", "3.14159", "2026-01-01"])
def test_non_string_scalars_are_checked(tmp_path, value, run_cli):
    p = tmp_path / "n.yaml"
    p.write_text(f"name: x\nqueue: default\nenv:\n  DB_PASSWORD: {value}\nsteps:\n  - run: echo\n")
    code, out, _ = run_cli("-q", "default", "--disable", "DL301", "--disable", "DL501", "--format", "json", str(p))
    codes = [f["code"] for f in json.loads(out)["files"][0]["findings"]]
    assert codes == ["DL401"] and value not in out


# Passwords chosen to defeat scrubbing of finished text: too short to
# substring-replace, or changed by repr/JSON/annotation escaping.
TRICKY = {"short": "abc", "short-mixed": "Zq9", "escapes": "pa\\ss\nword9", "unicode": "pässwörd",
          "quotes": "it's\"q"}


def _every_echo_site(pw):
    q = json.dumps(pw)  # a JSON string is a valid YAML double-quoted scalar
    return f"""name: x
queue: {q}
timeout_sec: {q}
schedule:
  start:
    - {q}
    - at: {q}
  {q}: "0 1 * * *"
handler_on:
  {q}:
    run: ./n.sh
env:
  DB_PASSWORD: {q}
steps:
  - id: {q}
    run: echo
    timeout_sec: {q}
    retry_policy:
      limit: {q}
      interval_sec: {q}
      max_interval_sec: {q}
  - id: b
    foreach:
      items: [a]
      steps:
        - id: {q}
          run: echo
"""


def _forms(pw):
    return {pw, repr(pw)[1:-1], json.dumps(pw)[1:-1], json.dumps(json.dumps(pw))[1:-1],
            pw.replace("%", "%25").replace("\n", "%0A")}


@pytest.mark.parametrize("pw", TRICKY.values(), ids=TRICKY.keys())
@pytest.mark.parametrize("fmt", ["text", "json", "github"])
def test_no_echo_site_discloses_a_detected_secret(tmp_path, pw, fmt, run_cli):
    p = tmp_path / "dag.yaml"
    p.write_text(_every_echo_site(pw), encoding="utf-8")
    code, out, err = run_cli("-q", "default", "--format", fmt, "--now", NOW, str(p))
    assert code == 1
    assert "DL401" in out  # the secret is detected ...
    for form in _forms(pw):  # ... and appears in no form anywhere else
        assert form not in out + err, form


def test_numeric_secret_not_echoed_by_numeric_rules(tmp_path, run_cli):
    p = tmp_path / "dag.yaml"
    p.write_text("name: x\nqueue: default\nenv:\n  DB_PASSWORD: 7777\nsteps:\n  - id: s\n    run: echo\n"
                 "    retry_policy:\n      limit: 7777\n      interval_sec: 7777\n")
    _, out, _ = run_cli("-q", "default", "--format", "json", str(p))
    codes = sorted(f["code"] for f in json.loads(out)["files"][0]["findings"])
    assert "DL401" in codes and "DL601" in codes and "DL604" in codes
    assert "7777" not in out


def test_secret_in_second_document_withheld_in_first(tmp_path, run_cli):
    p = tmp_path / "dag.yaml"
    p.write_text('name: x\nqueue: "abc"\nsteps:\n  - run: echo\n---\nname: sub\nenv:\n  DB_PASSWORD: abc\n'
                 "steps:\n  - run: echo\n")
    _, out, _ = run_cli("-q", "default", "--format", "json", str(p))
    assert "DL101" in out and "abc" not in out


def test_non_secret_values_are_still_quoted(run_cli):
    from conftest import FIXTURES

    _, out, _ = run_cli("-c", POLICY, "--now", NOW, os.path.join(FIXTURES, "bad", "queue-typo.yaml"))
    assert "queue 'reprots' is not in the allowlist; did you mean 'reports'?" in out


def test_secret_used_as_a_key_is_withheld_from_paths(tmp_path, run_cli):
    p = tmp_path / "dag.yaml"
    p.write_text("name: x\nqueue: default\nenv:\n  DB_PASSWORD: Zq9\nparams:\n  - Zq9:\n      API_TOKEN: other\n"
                 "steps:\n  - run: echo\n")
    _, out, _ = run_cli("-q", "default", "--format", "json", str(p))
    assert [f["code"] for f in json.loads(out)["files"][0]["findings"]].count("DL401") == 2
    assert "Zq9" not in out
