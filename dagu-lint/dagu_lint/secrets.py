"""Inline-secret detection.

Three independent signals, each reported with its own code so a team can tune
them separately:

* a value under a secret-looking key (`*_TOKEN`, `password`, ...) that is a
  literal rather than a reference (`${VAR}`, `$VAR`, a backtick command);
* a substring matching a known credential format (cloud keys, PATs, PEM);
* a high-entropy token (Shannon entropy over a base64/base62-looking run).

Values are never echoed: findings name the location, the signal and a length.
"""

from __future__ import annotations

import math
import re

SECRET_KEY_RE = re.compile(
    r"(?:^|[_\-.])(?:pass(?:word|wd)?|pwd|secret|token|api[_\-]?key|apikey|access[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|auth|credentials?|bearer|session[_\-]?key|signing[_\-]?key)"
    r"(?:$|[_\-.])",
    re.IGNORECASE,
)

# A value that only points at a secret held elsewhere.
REFERENCE_RE = re.compile(
    r"^\s*(?:\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*|`[^`]+`|\$\([^)]*\))\s*$"
)

KNOWN_PATTERNS = [
    ("AWS access key id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_\-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("Slack webhook URL", re.compile(r"https://hooks\.slack\.com/services/T[A-Za-z0-9]+/B[A-Za-z0-9]+/[A-Za-z0-9]+")),
    ("Stripe live key", re.compile(r"\b[sr]k_live_[A-Za-z0-9]{16,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("OpenAI-style API key", re.compile(r"\bsk-(?:proj-|ant-)?[A-Za-z0-9_\-]{32,}\b")),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("JSON Web Token", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    ("password in URL", re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:([^\s/@$`{]{3,})@", re.IGNORECASE)),
]

TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{16,}")


def entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    counts: dict = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def is_reference(value: str) -> bool:
    return bool(REFERENCE_RE.match(value))


def secret_key(key: str) -> bool:
    return bool(SECRET_KEY_RE.search(key))


def known_pattern(value: str) -> str | None:
    for label, rx in KNOWN_PATTERNS:
        m = rx.search(value)
        if not m:
            continue
        if label == "password in URL" and is_reference(m.group(1)):
            continue
        return label
    return None


def _looks_random(tok: str) -> bool:
    # Real keys mix character classes; words, paths, UUIDs and hex digests
    # mostly do not (hex is left to the key-name rule).
    classes = sum(bool(re.search(rx, tok)) for rx in (r"[a-z]", r"[A-Z]", r"[0-9]"))
    if classes < 3:
        return False
    if re.fullmatch(r"[0-9a-fA-F\-]+", tok):
        return False
    return True


def high_entropy_tokens(value: str, min_len: int, threshold: float, allow: list) -> list[tuple[int, float]]:
    """(length, entropy) of each suspicious token; the token itself is not returned."""
    out = []
    for m in TOKEN_RE.finditer(value):
        tok = m.group(0).strip("=")
        if len(tok) < min_len or not _looks_random(tok):
            continue
        if any(rx.search(tok) for rx in allow):
            continue
        e = entropy(tok)
        if e >= threshold:
            out.append((len(tok), round(e, 2)))
    return out
