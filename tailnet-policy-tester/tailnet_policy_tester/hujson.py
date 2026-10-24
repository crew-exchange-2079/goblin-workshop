"""HuJSON ("human JSON") reader: JSON plus comments and trailing commas.

Tailscale policy files are HuJSON (github.com/tailscale/hujson). This reader
blanks out ``//`` and ``/* */`` comments and trailing commas, keeping every
newline and column, then hands the result to the standard ``json`` module, so
error positions still point at the original text.

Deliberate strictness beyond HuJSON: duplicate object keys are an error (JSON
leaves them undefined; Go keeps the last one, which silently drops a rule).
"""

from __future__ import annotations

import json

__all__ = ["HuJSONError", "loads", "standardize"]

MAX_BYTES = 4 * 1024 * 1024


class HuJSONError(ValueError):
    def __init__(self, msg: str, line: int = 0, col: int = 0):
        self.msg, self.line, self.col = msg, line, col
        super().__init__(f"line {line}, column {col}: {msg}" if line else msg)


def _pos(text: str, idx: int) -> tuple[int, int]:
    line = text.count("\n", 0, idx) + 1
    return line, idx - (text.rfind("\n", 0, idx) + 1) + 1


def standardize(text: str) -> str:
    """Return ``text`` with comments and trailing commas replaced by spaces."""
    out = list(text)
    n = len(text)
    i = 0
    last_comma = -1  # index of a comma that may turn out to be trailing
    prev = ""  # previous significant character outside comments
    while i < n:
        c = text[i]
        if c == '"':
            last_comma = -1
            prev = '"'
            i += 1
            while i < n and text[i] != '"':
                if text[i] == "\\":
                    i += 1
                elif text[i] == "\n":
                    raise HuJSONError("newline inside string", *_pos(text, i))
                i += 1
            if i >= n:
                raise HuJSONError("unterminated string", *_pos(text, n))
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out[i] = " "
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end < 0:
                raise HuJSONError("unterminated /* comment", *_pos(text, i))
            for j in range(i, end + 2):
                if out[j] != "\n":
                    out[j] = " "
            i = end + 2
        elif c == ",":
            # Only a comma that follows a value can be trailing; "[,]" and
            # "[1,,]" stay invalid.
            last_comma = i if prev not in ("", "[", "{", ",", ":") else -1
            prev = c
            i += 1
        elif c in "]}":
            if last_comma >= 0:
                out[last_comma] = " "
            last_comma = -1
            prev = c
            i += 1
        else:
            if not c.isspace():
                last_comma = -1
                prev = c
            i += 1
    return "".join(out)


def _no_dupes(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise HuJSONError(f"duplicate key {k!r}")
        obj[k] = v
    return obj


def loads(text: str | bytes):
    if isinstance(text, bytes):
        if len(text) > MAX_BYTES:
            raise HuJSONError(f"input larger than {MAX_BYTES} bytes")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as e:
            raise HuJSONError(f"not UTF-8: {e.reason} at byte {e.start}") from None
    if len(text) > MAX_BYTES:
        raise HuJSONError(f"input larger than {MAX_BYTES} bytes")
    if text.startswith("﻿"):
        text = text[1:]
    std = standardize(text)
    try:
        return json.loads(std, object_pairs_hook=_no_dupes,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError as e:
        raise HuJSONError(e.msg, e.lineno, e.colno) from None


def _reject_constant(name: str):
    raise HuJSONError(f"{name} is not valid JSON")
