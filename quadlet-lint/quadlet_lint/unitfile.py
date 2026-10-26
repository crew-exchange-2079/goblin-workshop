"""A small systemd unit-file reader that keeps line numbers.

It follows the parts of systemd.syntax(7) that Quadlet files use: [Section]
headers, Key=Value assignments, '#' and ';' comment lines, and trailing
backslash continuations (comment lines inside a continuation are skipped,
as systemd does). It never raises on bad input: syntax problems are
collected in ``UnitFile.errors`` so the linter can report them and keep going.
"""

import re
from dataclasses import dataclass, field

DIRECTIVE_RE = re.compile(r"^[#;]\s*quadlet-lint:\s*(disable|disable-next-line)\s*=\s*([A-Za-z0-9_,\s-]+)$")
SECTION_RE = re.compile(r"^\[([^\[\]\s]+)\]$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass
class Entry:
    section: str
    key: str
    value: str
    line: int


@dataclass
class UnitFile:
    path: str
    entries: list = field(default_factory=list)
    sections: dict = field(default_factory=dict)  # name -> first line number
    errors: list = field(default_factory=list)  # (line, message)
    disabled: set = field(default_factory=set)  # rule ids disabled for the whole file
    disabled_lines: dict = field(default_factory=dict)  # line -> set of rule ids

    def has_section(self, section):
        return section in self.sections

    def all(self, section, key):
        """Every entry for key, honouring systemd's "empty value resets the list"."""
        out = []
        for e in self.entries:
            if e.section == section and e.key == key:
                if e.value == "":
                    out = []
                else:
                    out.append(e)
        return out

    def last(self, section, key):
        """The effective (last) entry for a single-valued key, or None."""
        found = None
        for e in self.entries:
            if e.section == section and e.key == key:
                found = e
        return found

    def get(self, section, key, default=None):
        e = self.last(section, key)
        return default if e is None else e.value

    def in_section(self, section):
        return [e for e in self.entries if e.section == section]


def _parse_directive(text, unit, next_line):
    m = DIRECTIVE_RE.match(text)
    if not m:
        return
    ids = {p.strip().upper() for p in m.group(2).split(",") if p.strip()}
    if m.group(1) == "disable":
        unit.disabled |= ids
    else:
        unit.disabled_lines.setdefault(next_line, set()).update(ids)


def parse(text, path="<string>"):
    unit = UnitFile(path=path)
    if text.startswith("\ufeff"):
        text = text[1:]
    lines = text.splitlines()
    section = None
    pending_disable = set()
    i = 0
    while i < len(lines):
        lineno = i + 1
        raw = lines[i].strip()
        i += 1
        if not raw:
            continue
        if raw[0] in "#;":
            m = DIRECTIVE_RE.match(raw)
            if m and m.group(1) == "disable-next-line":
                pending_disable |= {p.strip().upper() for p in m.group(2).split(",") if p.strip()}
            else:
                _parse_directive(raw, unit, lineno + 1)
            continue
        # Join continuation lines; comment lines inside a continuation are dropped.
        logical = raw
        while logical.endswith("\\"):
            logical = logical[:-1] + " "
            while i < len(lines) and lines[i].strip()[:1] in ("#", ";"):
                i += 1
            if i >= len(lines):
                break
            logical += lines[i].strip()
            i += 1
        logical = logical.strip()
        if pending_disable:
            unit.disabled_lines.setdefault(lineno, set()).update(pending_disable)
            pending_disable = set()

        if logical.startswith("["):
            m = SECTION_RE.match(logical)
            if not m:
                unit.errors.append((lineno, f"malformed section header {logical!r}"))
                section = None
                continue
            section = m.group(1)
            unit.sections.setdefault(section, lineno)
            continue
        if "=" not in logical:
            unit.errors.append((lineno, f"line is not a Key=Value assignment: {logical!r}"))
            continue
        key, value = logical.split("=", 1)
        key, value = key.strip(), value.strip()
        if not KEY_RE.match(key):
            unit.errors.append((lineno, f"invalid key name {key!r}"))
            continue
        if section is None:
            unit.errors.append((lineno, f"assignment {key}= appears before any [Section] header"))
            continue
        unit.entries.append(Entry(section, key, value, lineno))
    return unit
