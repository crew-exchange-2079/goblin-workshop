#!/usr/bin/env python3
"""timer2dagu: convert a systemd .timer + .service pair into a Dagu DAG.

Standard library only. The DAG is written as YAML; everything that could not
be carried over exactly is listed in a report, under one of three headings:

  untranslated  the directive has no Dagu equivalent and was dropped (or, for
                specifiers, left verbatim); behaviour WILL differ
  approximated  translated, but the Dagu behaviour is close rather than equal
  ignored       no effect for a timer-started job under Dagu; nothing lost

Environment= values are never copied into the DAG: they go to a companion
.env file that the DAG names in `dotenv`. EnvironmentFile= paths are named in
`dotenv` as-is and never read.
"""

import argparse
import json
import math
import os
import re
import sys

__version__ = "0.1.0"

UNTRANSLATED = "untranslated"
APPROXIMATED = "approximated"
IGNORED = "ignored"
_KINDS = (UNTRANSLATED, APPROXIMATED, IGNORED)


class ConvertError(Exception):
    """The input cannot be converted at all (as opposed to a report entry)."""


class Report:
    def __init__(self):
        self.items = []  # (kind, where, message)

    def add(self, kind, where, message):
        entry = (kind, where, message)
        if entry not in self.items:
            self.items.append(entry)

    def has(self, kind):
        return any(k == kind for k, _, _ in self.items)

    def render(self, title, files=()):
        """Group by kind; within a group, order by file (in `files` order) and line."""
        def order(item):
            m = re.match(r"([^\s:]+)(?::(\d+))?", item[1])
            name = m.group(1) if m else ""
            rank = files.index(name) if name in files else len(files)
            return (rank, int(m.group(2)) if m and m.group(2) else 0)

        lines = ["timer2dagu report: " + title]
        if not self.items:
            lines.append("nothing to report: every directive was translated")
        for kind in _KINDS:
            group = [(w, m) for k, w, m in sorted(self.items, key=order) if k == kind]
            if not group:
                continue
            lines.append("")
            lines.append("%s (%d):" % (kind, len(group)))
            for where, message in group:
                lines.append("  %s: %s" % (where, message))
        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Unit file syntax (systemd.syntax(7))
# ---------------------------------------------------------------------------

class Directive:
    def __init__(self, section, key, value, filename, line):
        self.section = section
        self.key = key
        self.value = value
        self.filename = filename
        self.line = line

    @property
    def where(self):
        return "%s:%d %s=" % (self.filename, self.line, self.key)


def parse_unit(text, filename):
    """Return the directives of a unit file in order, continuation lines joined."""
    out = []
    section = None
    pending = None  # [start_line, text]
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if pending is None:
            if not line or line[0] in "#;":
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1]
                continue
            pending = [lineno, ""]
        elif line[:1] in ("#", ";"):
            continue  # comment lines inside a continuation are skipped
        if line.endswith("\\"):
            pending[1] += line[:-1] + " "
            continue
        pending[1] += line
        start, joined = pending
        pending = None
        if "=" not in joined:
            raise ConvertError("%s:%d: not a Key=Value line: %r" % (filename, start, joined))
        if section is None:
            raise ConvertError("%s:%d: assignment outside any [Section]" % (filename, start))
        key, value = joined.split("=", 1)
        out.append(Directive(section, key.strip(), value.strip(), filename, start))
    if pending is not None:
        start, joined = pending
        key, _, value = joined.partition("=")
        out.append(Directive(section, key.strip(), value.strip(), filename, start))
    return out


# ---------------------------------------------------------------------------
# Specifiers (systemd.unit(5) "Specifiers")
# ---------------------------------------------------------------------------

def unescape_unit(s):
    """Undo systemd-escape: '-' is '/', and \\xNN is a byte."""
    s = s.replace("-", "/")
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), s)


class Context:
    """What specifiers resolve to for one unit."""

    def __init__(self, unit_name, user, home, user_name, report):
        self.unit_name = unit_name
        self.user = user
        self.home = home
        self.user_name = user_name
        self.report = report

    def _names(self):
        stem = self.unit_name.rsplit(".", 1)[0]
        prefix, at, instance = stem.partition("@")
        return stem, prefix, instance

    def _home(self):
        if not self.user:
            return ("lit", "/root")
        if self.home:
            return ("lit", self.home)
        return ("var", "HOME")

    def _under_home(self, rel):
        kind, val = self._home()
        if kind == "lit":
            return [("lit", val.rstrip("/") + "/" + rel)]
        return [("var", val), ("lit", "/" + rel)]

    def resolve(self, ch, where):
        """Return a list of segments, or None when the specifier is unknown."""
        stem, prefix, instance = self._names()
        fixed = {
            "%": "%",
            "n": self.unit_name,
            "N": stem,
            "p": prefix,
            "P": unescape_unit(prefix),
            "i": instance,
            "I": unescape_unit(instance),
            "j": prefix.rsplit("-", 1)[-1],
            "J": unescape_unit(prefix.rsplit("-", 1)[-1]),
            "f": "/" + unescape_unit(instance or prefix).lstrip("/"),
            "T": "/tmp",
            "V": "/var/tmp",
        }
        if ch in fixed:
            return [("lit", fixed[ch])]
        if not self.user:
            system = {"h": "/root", "u": "root", "U": "0", "g": "root", "G": "0",
                      "s": "/bin/sh", "t": "/run", "S": "/var/lib", "C": "/var/cache",
                      "L": "/var/log", "E": "/etc"}
            if ch in system:
                return [("lit", system[ch])]
            return None
        if ch == "h":
            kind, val = self._home()
            if kind == "var":
                self._note_runtime(ch, "${HOME}", where)
            return [self._home()]
        if ch == "u":
            if self.user_name:
                return [("lit", self.user_name)]
            self._note_runtime(ch, "${USER}", where)
            return [("var", "USER")]
        if ch == "t":
            self._note_runtime(ch, "${XDG_RUNTIME_DIR}", where)
            return [("var", "XDG_RUNTIME_DIR")]
        rel = {"S": ".local/state", "C": ".cache", "L": ".local/state/log", "E": ".config"}
        if ch in rel:
            if not self.home:
                self.report.add(APPROXIMATED, where,
                                "%%%s used systemd's default ~/%s; $XDG_* overrides are not "
                                "consulted, and the path is taken from ${HOME} at run time "
                                "(pass --home to fix it)" % (ch, rel[ch]))
            else:
                self.report.add(APPROXIMATED, where,
                                "%%%s used systemd's default ~/%s; $XDG_* overrides are "
                                "not consulted" % (ch, rel[ch]))
            return self._under_home(rel[ch])
        return None

    def _note_runtime(self, ch, ref, where):
        self.report.add(APPROXIMATED, where,
                        "%%%s became %s, which Dagu or the shell reads from the Dagu process "
                        "environment at run time, not from the user manager" % (ch, ref)
                        + (" (pass --home to fix it)" if ch == "h" else ""))


# ---------------------------------------------------------------------------
# Command-line and value splitting (systemd.service(5) "Command lines")
# ---------------------------------------------------------------------------

_C_ESCAPES = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
              "v": "\v", "\\": "\\", '"': '"', "'": "'", "s": " "}
_VARNAME = r"[A-Za-z_][A-Za-z0-9_]*"


def split_words(value, ctx, where, expand_env):
    """Split a systemd value into words.

    Each word is a list of segments:
      ("lit", text)    literal text
      ("var", NAME)    one-word variable reference (${NAME}, or a specifier)
      ("split", NAME)  $NAME as a whole word: split at whitespace by systemd
    """
    words = []
    i, n = 0, len(value)
    while True:
        while i < n and value[i] in " \t\n":
            i += 1
        if i >= n:
            break
        word = []
        lit = []
        quote = None

        def flush():
            if lit:
                word.append(("lit", "".join(lit)))
                del lit[:]

        while i < n:
            c = value[i]
            if quote is None and c in " \t\n":
                break
            if c in "\"'" and (quote is None or quote == c):
                quote = None if quote else c
                i += 1
                continue
            if c == "\\" and i + 1 < n:
                e = value[i + 1]
                if e in _C_ESCAPES:
                    lit.append(_C_ESCAPES[e])
                    i += 2
                    continue
                m = re.match(r"x([0-9a-fA-F]{2})", value[i + 1:])
                if m:
                    lit.append(chr(int(m.group(1), 16)))
                    i += 2 + len(m.group(0)) - 1
                    continue
                m = re.match(r"[0-7]{3}", value[i + 1:])
                if m:
                    lit.append(chr(int(m.group(0), 8)))
                    i += 4
                    continue
                lit.append(e)
                i += 2
                continue
            if c == "%" and i + 1 < n:
                segs = ctx.resolve(value[i + 1], where)
                if segs is None:
                    ctx.report.add(UNTRANSLATED, where,
                                   "specifier %%%s has no translation and was left verbatim"
                                   % value[i + 1])
                    segs = [("lit", "%" + value[i + 1])]
                for kind, val in segs:
                    if kind == "lit":
                        lit.append(val)
                    else:
                        flush()
                        word.append((kind, val))
                i += 2
                continue
            if c == "$" and expand_env:
                if value.startswith("$$", i):
                    lit.append("$")
                    i += 2
                    continue
                m = re.match(r"\$\{(" + _VARNAME + r")\}", value[i:])
                if m:
                    flush()
                    word.append(("var", m.group(1)))
                    i += len(m.group(0))
                    continue
                m = re.match(r"\$(" + _VARNAME + r")", value[i:])
                if m and not word and not lit:
                    end = i + len(m.group(0))
                    after = end + 1 if quote and value[end:end + 1] == quote else end
                    if after >= n or value[after] in " \t\n":
                        # $NAME as a whole word: systemd splits it at whitespace
                        word.append(("split", m.group(1)))
                        if after != end:
                            quote = None
                        i = after
                        continue
            lit.append(c)
            i += 1
        if quote is not None:
            raise ConvertError("%s unterminated %s quote" % (where, quote))
        flush()
        words.append(word or [("lit", "")])
    return words


def spec_word(value, ctx, where):
    """A path value: specifiers are resolved, nothing is split or unquoted."""
    word, lit = [], []
    i = 0
    while i < len(value):
        if value[i] == "%" and i + 1 < len(value):
            segs = ctx.resolve(value[i + 1], where)
            if segs is None:
                ctx.report.add(UNTRANSLATED, where,
                               "specifier %%%s has no translation and was left verbatim"
                               % value[i + 1])
                segs = [("lit", value[i:i + 2])]
            for kind, val in segs:
                if kind == "lit":
                    lit.append(val)
                else:
                    if lit:
                        word.append(("lit", "".join(lit)))
                        lit = []
                    word.append((kind, val))
            i += 2
        else:
            lit.append(value[i])
            i += 1
    if lit:
        word.append(("lit", "".join(lit)))
    return word


def word_is_literal(word, text):
    return len(word) == 1 and word[0] == ("lit", text)


_SH_SAFE = re.compile(r"[A-Za-z0-9_@%+=:,./-]+")


def sh_word(word):
    parts = []
    for kind, val in word:
        if kind == "lit":
            if _SH_SAFE.fullmatch(val):
                parts.append(val)
            else:
                parts.append("'" + val.replace("'", "'\\''") + "'")
        elif kind == "var":
            parts.append('"${%s}"' % val)
        else:
            parts.append("$" + val)
    return "".join(parts) if parts else "''"


def plain_text(word):
    """Render a word for a Dagu field that is not run by a shell (paths)."""
    return "".join(val if kind == "lit" else "${%s}" % val for kind, val in word)


# ---------------------------------------------------------------------------
# Time spans (systemd.time(7))
# ---------------------------------------------------------------------------

_SPAN_UNITS = [
    (("usec", "us", "\u00b5s", "\u03bcs"), 1e-6),
    (("msec", "ms"), 1e-3),
    (("seconds", "second", "sec", "s"), 1.0),
    (("minutes", "minute", "min", "m"), 60.0),
    (("hours", "hour", "hr", "h"), 3600.0),
    (("days", "day", "d"), 86400.0),
    (("weeks", "week", "w"), 604800.0),
    (("months", "month", "M"), 2629800.0),
    (("years", "year", "y"), 31557600.0),
]


def parse_timespan(text):
    """Seconds as a float, math.inf for 'infinity', None if unparsable."""
    t = text.strip()
    if t == "infinity":
        return math.inf
    if not t:
        return None
    total = 0.0
    pos = 0
    pattern = re.compile(r"\s*(\d+(?:\.\d*)?|\.\d+)\s*([A-Za-z\u00b5\u03bc]*)")
    while pos < len(t):
        m = pattern.match(t, pos)
        if not m or m.end() == pos:
            return None
        num, unit = float(m.group(1)), m.group(2)
        if unit == "":
            mult = 1.0
        else:
            mult = None
            for names, factor in _SPAN_UNITS:
                if unit in names:
                    mult = factor
                    break
            if mult is None:
                return None
        total += num * mult
        pos = m.end()
        while pos < len(t) and t[pos] == " ":
            pos += 1
    return total


# ---------------------------------------------------------------------------
# OnCalendar (systemd.time(7) "Calendar Events") -> 5-field cron
# ---------------------------------------------------------------------------

_SHORTHANDS = {
    "minutely": "*-*-* *:*:00",
    "hourly": "*-*-* *:00:00",
    "daily": "*-*-* 00:00:00",
    "monthly": "*-*-01 00:00:00",
    "weekly": "Mon *-*-* 00:00:00",
    "yearly": "*-01-01 00:00:00",
    "annually": "*-01-01 00:00:00",
    "quarterly": "*-01,04,07,10-01 00:00:00",
    "semiannually": "*-01,07-01 00:00:00",
}

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_DAY_NAMES = {d: i for i, d in enumerate(_DAYS)}
_DAY_NAMES.update({full: i for i, full in enumerate(
    ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"])})


class CalendarError(Exception):
    def __init__(self, kind, message):
        Exception.__init__(self, message)
        self.kind = kind


def _bad(message):
    return CalendarError(UNTRANSLATED, message)


def _is_timezone(tok):
    if tok in ("UTC", "GMT", "Z"):
        return True
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)+$", tok))


def _parse_weekdays(tok):
    days = set()
    for item in tok.split(","):
        if not item:
            raise _bad("empty weekday list item in %r" % tok)
        lo, sep, hi = item.partition("..")
        if not sep:
            lo, sep, hi = item.partition("-")
        try:
            a = _DAY_NAMES[lo.lower()]
            b = _DAY_NAMES[hi.lower()] if sep else a
        except KeyError:
            raise _bad("unknown weekday in %r" % tok)
        if b < a:
            raise _bad("weekday range %r runs backwards" % item)
        days.update(range(a, b + 1))
    return days


def _cron_weekdays(days):
    # systemd Mon=0..Sun=6 -> cron Sun=0, Mon=1..Sat=6
    nums = sorted((d + 1) % 7 for d in days)
    if len(nums) == 7:
        return "*"
    runs = []
    for n in nums:
        if runs and n == runs[-1][1] + 1:
            runs[-1][1] = n
        else:
            runs.append([n, n])
    return ",".join(str(a) if a == b else ("%d,%d" % (a, b) if b == a + 1 else "%d-%d" % (a, b))
                    for a, b in runs)


def _component(text, lo, hi, label):
    """Translate one systemd date/time component into a cron field."""
    if text == "*":
        return "*"
    items = []
    for item in text.split(","):
        m = re.match(r"^(\*|\d+)(?:\.\.(\d+))?(?:/(\d+))?$", item)
        if not m:
            raise _bad("%s value %r is not supported" % (label, item))
        start, end, step = m.group(1), m.group(2), m.group(3)
        if start == "*":
            if end is not None:
                raise _bad("%s value %r is not supported" % (label, item))
            if step is None:
                return "*"
            if int(step) == 0:
                raise _bad("%s repetition of 0 in %r" % (label, item))
            items.append("*/%d" % int(step))
            continue
        a = int(start)
        b = int(end) if end is not None else None
        for v in (a, b):
            if v is not None and not lo <= v <= hi:
                raise _bad("%s %d is out of range %d..%d" % (label, v, lo, hi))
        if b is not None and b < a:
            raise _bad("%s range %r runs backwards" % (label, item))
        if step is not None and int(step) == 0:
            raise _bad("%s repetition of 0 in %r" % (label, item))
        s = "%d" % a
        if b is not None:
            s += "-%d" % b
        if step is not None:
            s += "/%d" % int(step)
        items.append(s)
    return ",".join(items)


def _restricted(field):
    return field != "*"


def calendar_to_cron(expr):
    """Return (cron, notes) where notes are (kind, message) approximation notes.

    Raises CalendarError when the expression cannot be expressed in cron.
    """
    notes = []
    text = " ".join(expr.split())
    if not text:
        raise _bad("empty expression")
    toks = text.split(" ")
    tz = None
    if len(toks) > 1 and _is_timezone(toks[-1]):
        tz = toks.pop()
    if len(toks) == 1 and toks[0].lower() in _SHORTHANDS:
        toks = _SHORTHANDS[toks[0].lower()].split(" ")
    if toks and toks[0].startswith("@"):
        raise _bad("a UNIX-timestamp event (%s) fires once; not translated" % toks[0])

    weekdays = None
    if toks and re.match(r"^[A-Za-z]", toks[0]):
        weekdays = _parse_weekdays(toks.pop(0))
    date = time = None
    for tok in toks:
        if ":" in tok and time is None:
            time = tok
        elif ("-" in tok or "~" in tok) and date is None and ":" not in tok:
            date = tok
        else:
            raise _bad("cannot parse %r" % tok)
    if date is None:
        date = "*-*-*"
    if time is None:
        time = "00:00:00"

    if "~" in date:
        raise _bad("'~' (counting days from the end of the month) has no cron equivalent")
    parts = date.split("-")
    if len(parts) == 2:
        parts = ["*"] + parts
    if len(parts) != 3:
        raise _bad("cannot parse date %r" % date)
    year, month, day = parts
    if year != "*":
        raise _bad("a specific year (%s) has no 5-field cron equivalent" % year)

    tparts = time.split(":")
    if len(tparts) == 2:
        tparts.append("00")
    if len(tparts) != 3:
        raise _bad("cannot parse time %r" % time)
    hour, minute, second = tparts

    if second not in ("0", "00", "0.0", "00.000000"):
        m = re.match(r"^(\d+)(\.\d+)?$", second)
        if m and 0 <= int(m.group(1)) <= 59:
            notes.append((APPROXIMATED,
                          "seconds %s dropped: cron fires at second 0 of the minute" % second))
        else:
            raise _bad("seconds %r: cron cannot fire more than once a minute" % second)

    cron_min = _component(minute, 0, 59, "minute")
    cron_hour = _component(hour, 0, 23, "hour")
    cron_dom = _component(day, 1, 31, "day")
    cron_mon = _component(month, 1, 12, "month")
    cron_dow = "*" if weekdays is None else _cron_weekdays(weekdays)
    if _restricted(cron_dom) and _restricted(cron_dow):
        raise _bad("both weekday and day-of-month are restricted; systemd requires both "
                   "to match, cron fires when either matches")
    cron = " ".join([cron_min, cron_hour, cron_dom, cron_mon, cron_dow])
    if tz:
        cron = "CRON_TZ=%s %s" % (tz, cron)
    return cron, notes


# ---------------------------------------------------------------------------
# YAML output (a small deterministic emitter; no dependency)
# ---------------------------------------------------------------------------

_PLAIN = re.compile(r"[A-Za-z_./][A-Za-z0-9_./@=+-]*")
_YAML_RESERVED = {"y", "n", "yes", "no", "on", "off", "true", "false", "null", "~"}


def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if _PLAIN.fullmatch(v) and v.lower() not in _YAML_RESERVED:
        return v
    return json.dumps(v, ensure_ascii=False)


def _block_ok(v):
    """A multi-line string that a |- block scalar reproduces exactly."""
    lines = v.split("\n")
    return (len(lines) > 1 and lines[0][:1] not in (" ", "\t")
            and all(ln == ln.rstrip(" \t") and "\r" not in ln for ln in lines)
            and lines[-1] != "")


def _emit(v, indent, out):
    pad = "  " * indent
    if isinstance(v, dict):
        for k, val in v.items():
            if isinstance(val, (dict, list)) and val:
                out.append("%s%s:" % (pad, _scalar(k)))
                _emit(val, indent + (1 if isinstance(val, dict) else 0), out)
            elif isinstance(val, str) and _block_ok(val):
                out.append("%s%s: |-" % (pad, _scalar(k)))
                for ln in val.split("\n"):
                    out.append(("%s  %s" % (pad, ln)) if ln else "")
            else:
                out.append("%s%s: %s" % (pad, _scalar(k), _scalar(val) if val != [] else "[]"))
    else:
        for item in v:
            if isinstance(item, dict):
                sub = []
                _emit(item, indent + 1, sub)
                first = sub[0][len(pad) + 2:]
                out.append("%s- %s" % (pad, first))
                out.extend(sub[1:])
            else:
                out.append("%s- %s" % (pad, _scalar(item)))


def to_yaml(doc):
    out = []
    _emit(doc, 0, out)
    return "\n".join(out) + "\n"


def dotenv_value(word):
    """Quote a value for a godotenv file, keeping variable references live."""
    if all(k == "lit" for k, _ in word):
        text = "".join(v for _, v in word)
        if "'" not in text and "\n" not in text:
            return "'" + text + "'"
    out = []
    for kind, val in word:
        if kind == "lit":
            out.append(val.replace("\\", "\\\\").replace('"', '\\"')
                       .replace("$", "\\$").replace("\n", "\\n"))
        else:
            out.append("${%s}" % val)
    return '"' + "".join(out) + '"'


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------

_TIMER_IGNORED = {
    "Description": None,
    "Documentation": None,
    "RemainAfterElapse": "only affects when systemd unloads the timer",
}
_TIMER_UNTRANSLATED = {
    "OnBootSec": "relative to boot; Dagu has no boot-time trigger",
    "OnStartupSec": "relative to service-manager start-up; Dagu has no equivalent",
    "OnActiveSec": "relative to timer activation; Dagu has no equivalent",
    "WakeSystem": "Dagu cannot wake a suspended system",
    "OnClockChange": "Dagu has no clock-change trigger",
    "OnTimezoneChange": "Dagu has no time-zone-change trigger",
    "FixedRandomDelay": "the delay is drawn afresh on every run instead of being fixed per host",
    "DeferReactivation": "Dagu has no equivalent",
}
_UNIT_DEPENDENCIES = {"After", "Before", "Requires", "Wants", "BindsTo", "PartOf",
                      "Requisite", "Conflicts", "Upholds", "OnFailure", "OnSuccess"}
_SERVICE_DIRECTIVES = {"Type", "ExecStartPre", "ExecStart", "ExecStartPost",
                       "ExecStopPost", "Environment", "EnvironmentFile", "WorkingDirectory",
                       "TimeoutStartSec", "TimeoutSec", "RuntimeMaxSec"}
_SERVICE_IGNORED = {
    "RemainAfterExit": "only affects how systemd reports the unit after it exits",
    "StandardOutput": "Dagu captures step output in its own logs",
    "StandardError": "Dagu captures step output in its own logs",
    "SyslogIdentifier": "Dagu captures step output in its own logs",
}


def _is_user_path(path):
    p = path.replace(os.sep, "/")
    return "/systemd/user/" in p or "/.config/systemd/user" in p


class _Step:
    def __init__(self, name, run, origin):
        self.name = name
        self.run = run
        self.origin = origin
        self.continue_on_failure = False
        self.timeout = None


def _command(d, ctx, report):
    """Translate one Exec*= value into (shell text, ignore_failure)."""
    value = d.value
    prefixes = ""
    while value and value[0] in "@-:+!|":
        prefixes += value[0]
        value = value[1:]
    value = value.lstrip()
    ignore = "-" in prefixes
    no_env = ":" in prefixes
    if "|" in prefixes:
        report.add(APPROXIMATED, d.where,
                   "'|' (run via the login shell) became a plain shell command; the login "
                   "shell's profile is not sourced")
    if "+" in prefixes or "!" in prefixes:
        report.add(APPROXIMATED, d.where,
                   "privilege prefix '%s' has no Dagu meaning: the command runs as the Dagu "
                   "process user" % ("!!" if "!!" in prefixes else ("!" if "!" in prefixes else "+")))
    if "|" in prefixes:
        return value, ignore
    words = split_words(value, ctx, d.where, expand_env=not no_env)
    if not words:
        raise ConvertError("%s empty command" % d.where)
    if "@" in prefixes:
        if len(words) < 2:
            raise ConvertError("%s '@' needs an argv[0] after the executable" % d.where)
        report.add(APPROXIMATED, d.where,
                   "'@' argv[0] override dropped: the program sees its real name")
        words = [words[0]] + words[2:]
    if any(word_is_literal(w, ";") for w in words):
        report.add(APPROXIMATED, d.where,
                   "';'-separated commands were joined with '&&' in one step")
    parts = []
    for w in words:
        parts.append("&&" if word_is_literal(w, ";") else sh_word(w))
    for w in words:
        for kind, val in w:
            if kind == "lit" and "$" in val:
                report.add(APPROXIMATED, d.where,
                           "a literal '$' is single-quoted for the shell, but Dagu may still "
                           "interpolate it before the shell runs")
                break
    return " ".join(parts), ignore


def convert(timer_text, timer_name, service_text=None, service_name=None, user=False,
            home=None, user_name=None, catchup_window="7d", env_file="", inline_env=False):
    """Convert one timer/service pair.

    Returns (yaml_text, env_text_or_None, report_text, report).
    `env_file` is the path written into `dotenv` for Environment= values.
    """
    report = Report()
    timer = parse_unit(timer_text, timer_name)
    stem = timer_name.rsplit(".", 1)[0]

    # ---- timer ----
    schedules = []
    persistent = False
    random_delay = 0.0
    unit_target = None
    descriptions = {}
    for d in timer:
        if d.section == "Install":
            report.add(IGNORED, d.where, "[Install] does not apply: Dagu schedules the DAG itself")
            continue
        if d.section == "Unit":
            if d.key == "Description":
                descriptions["timer"] = d.value
            elif d.key == "Documentation":
                pass
            elif d.key in _UNIT_DEPENDENCIES:
                report.add(UNTRANSLATED, d.where, "unit dependencies have no Dagu equivalent")
            else:
                report.add(UNTRANSLATED, d.where, "no Dagu equivalent")
            continue
        if d.section != "Timer":
            report.add(UNTRANSLATED, d.where, "unknown section [%s]" % d.section)
            continue
        if d.key == "OnCalendar":
            if not d.value:
                schedules = []
                continue
            try:
                cron, notes = calendar_to_cron(d.value)
            except CalendarError as e:
                report.add(e.kind, d.where, "%r: %s" % (d.value, e))
                continue
            for kind, message in notes:
                report.add(kind, d.where, "%r: %s" % (d.value, message))
            if cron not in schedules:
                schedules.append(cron)
        elif d.key == "Persistent":
            persistent = _bool(d)
        elif d.key == "RandomizedDelaySec":
            span = parse_timespan(d.value)
            if span is None or span == math.inf:
                report.add(UNTRANSLATED, d.where, "cannot parse time span %r" % d.value)
            else:
                random_delay = span
        elif d.key == "AccuracySec":
            report.add(IGNORED, d.where,
                       "Dagu's scheduler fires on the minute; systemd's coalescing window "
                       "does not apply")
        elif d.key == "Unit":
            unit_target = d.value
        elif d.key in ("OnUnitActiveSec", "OnUnitInactiveSec"):
            span = parse_timespan(d.value)
            if span is None or span == math.inf or span < 60:
                report.add(UNTRANSLATED, d.where,
                           "%r: cannot express as a Dagu '@every' interval" % d.value)
                continue
            # Go durations have no 'd' unit, so @every counts in hours.
            every = "@every %s" % _go_hms(span)
            report.add(APPROXIMATED, d.where,
                       "%r became %r: systemd counts from the service's last %s, Dagu "
                       "from its scheduler's own clock" % (
                           d.value, every,
                           "activation" if d.key == "OnUnitActiveSec" else "deactivation"))
            if every not in schedules:
                schedules.append(every)
        elif d.key in _TIMER_UNTRANSLATED:
            report.add(UNTRANSLATED, d.where, _TIMER_UNTRANSLATED[d.key])
        elif d.key in _TIMER_IGNORED:
            if d.key == "Description":
                descriptions["timer"] = d.value
            elif _TIMER_IGNORED[d.key]:
                report.add(IGNORED, d.where, _TIMER_IGNORED[d.key])
        else:
            report.add(UNTRANSLATED, d.where, "unknown [Timer] directive")

    if service_name is None:
        service_name = unit_target or (stem + ".service")
    elif unit_target and unit_target != service_name:
        report.add(APPROXIMATED, "%s Unit=" % timer_name,
                   "timer names %s but %s was converted" % (unit_target, service_name))
    if service_text is None:
        raise ConvertError("no service unit given for %s (expected %s)" % (timer_name, service_name))

    ctx = Context(service_name, user, home, user_name, report)
    service = parse_unit(service_text, service_name)

    # ---- service ----
    stype = "simple"
    execs = {"ExecStartPre": [], "ExecStart": [], "ExecStartPost": [], "ExecStopPost": []}
    env_assign = []  # (key, word, where)
    env_files = []   # (path, optional, where)
    working_dir = None
    timeout_start = None
    runtime_max = None
    for d in service:
        if d.section == "Install":
            report.add(IGNORED, d.where, "[Install] does not apply: the timer starts the job")
            continue
        if d.section == "Unit":
            if d.key == "Description":
                descriptions["service"] = d.value
            elif d.key == "Documentation":
                pass
            elif d.key in _UNIT_DEPENDENCIES:
                report.add(UNTRANSLATED, d.where, "unit dependencies have no Dagu equivalent")
            else:
                report.add(UNTRANSLATED, d.where, "no Dagu equivalent")
            continue
        if d.section != "Service":
            report.add(UNTRANSLATED, d.where, "unknown section [%s]" % d.section)
            continue
        if d.key in execs:
            if not d.value:
                execs[d.key] = []
            else:
                execs[d.key].append(d)
        elif d.key == "Type":
            stype = d.value
            if stype in ("forking", "notify", "notify-reload", "dbus"):
                report.add(UNTRANSLATED, d.where,
                           "Type=%s readiness semantics do not exist in Dagu; each step simply "
                           "runs until its process exits" % stype)
            elif stype == "idle":
                report.add(IGNORED, d.where, "Type=idle only delays output until boot settles")
            elif stype not in ("simple", "exec", "oneshot"):
                report.add(UNTRANSLATED, d.where, "unknown Type=%s" % stype)
        elif d.key == "Environment":
            if not d.value:
                env_assign = []
                continue
            for w in split_words(d.value, ctx, d.where, expand_env=False):
                assignment = _split_assignment(w)
                if assignment is None:
                    report.add(IGNORED, d.where,
                               "%r is not a valid KEY=value assignment; systemd ignores it too"
                               % plain_text(w))
                else:
                    env_assign.append(assignment + (d.where,))
        elif d.key == "EnvironmentFile":
            if not d.value:
                env_files = []
                continue
            optional = d.value.startswith("-")
            raw = d.value[1:] if optional else d.value
            env_files.append((plain_text(spec_word(raw, ctx, d.where)), optional, d.where))
        elif d.key == "WorkingDirectory":
            raw = d.value
            optional = raw.startswith("-")
            if optional:
                raw = raw[1:]
                report.add(APPROXIMATED, d.where,
                           "'-' (ignore a missing directory) has no Dagu equivalent; a missing "
                           "directory fails the run")
            if raw == "~":
                kind, val = ctx._home()
                working_dir = val if kind == "lit" else "${%s}" % val
                if kind != "lit":
                    report.add(APPROXIMATED, d.where,
                               "'~' became ${HOME}, read from the Dagu process environment "
                               "(pass --home to fix it)")
            else:
                working_dir = plain_text(spec_word(raw, ctx, d.where))
        elif d.key in ("TimeoutStartSec", "TimeoutSec"):
            span = parse_timespan(d.value)
            if span is None:
                report.add(UNTRANSLATED, d.where, "cannot parse time span %r" % d.value)
            else:
                timeout_start = (math.inf if span == 0 else span, d)
            if d.key == "TimeoutSec":
                report.add(UNTRANSLATED, d.where,
                           "the stop half of TimeoutSec= (TimeoutStopSec) has no Dagu "
                           "equivalent; only the start timeout was translated")
        elif d.key == "RuntimeMaxSec":
            span = parse_timespan(d.value)
            if span is None:
                report.add(UNTRANSLATED, d.where, "cannot parse time span %r" % d.value)
            else:
                runtime_max = (math.inf if span == 0 else span, d)
        elif d.key in _SERVICE_IGNORED:
            report.add(IGNORED, d.where, _SERVICE_IGNORED[d.key])
        elif d.key in ("User", "Group", "DynamicUser", "SupplementaryGroups"):
            report.add(UNTRANSLATED, d.where,
                       "Dagu runs steps as the Dagu process user; run the DAG under that "
                       "account or wrap the commands (e.g. with runuser)")
        elif d.key == "ExecCondition":
            report.add(UNTRANSLATED, d.where,
                       "not translated; Dagu `preconditions` have different exit-code "
                       "semantics (systemd skips on 1-254, fails on 255)")
        elif d.key.startswith("Exec"):
            report.add(UNTRANSLATED, d.where, "has no meaning for a run-to-completion job")
        else:
            report.add(UNTRANSLATED, d.where, "no Dagu equivalent")

    if not execs["ExecStart"]:
        raise ConvertError("%s has no ExecStart=" % service_name)
    if len(execs["ExecStart"]) > 1 and stype != "oneshot":
        report.add(APPROXIMATED, execs["ExecStart"][1].where,
                   "systemd accepts several ExecStart= only for Type=oneshot; they were "
                   "translated as sequential steps")

    # ---- steps ----
    steps = []
    if random_delay > 0:
        n = int(math.ceil(random_delay))
        steps.append(_Step("randomized-delay",
                           "sleep $(( $(od -An -N4 -tu4 /dev/urandom) %% %d ))" % (n + 1),
                           "RandomizedDelaySec=%s" % _timer_value(timer, "RandomizedDelaySec")))
        report.add(APPROXIMATED, "%s RandomizedDelaySec=" % timer_name,
                   "became a first step sleeping 0..%ds; the delay shows as a step and "
                   "counts towards the DAG's run time" % n)
    labels = {"ExecStartPre": "exec-start-pre", "ExecStart": "exec-start",
              "ExecStartPost": "exec-start-post"}
    for key in ("ExecStartPre", "ExecStart", "ExecStartPost"):
        lst = execs[key]
        for idx, d in enumerate(lst, 1):
            run, ignore = _command(d, ctx, report)
            name = labels[key] if len(lst) == 1 else "%s-%d" % (labels[key], idx)
            step = _Step(name, run, "%s=%s" % (key, d.value))
            step.continue_on_failure = ignore
            steps.append(step)

    handler = None
    if execs["ExecStopPost"]:
        lines = []
        for d in execs["ExecStopPost"]:
            run, ignore = _command(d, ctx, report)
            lines.append(run + (" || true" if ignore else ""))
        if len(lines) == 1:
            handler = lines[0]
        else:
            handler = "set -e\n" + "\n".join(lines)
        report.add(APPROXIMATED, execs["ExecStopPost"][0].where,
                   "ExecStopPost= became handler_on.exit; $SERVICE_RESULT, $EXIT_CODE and "
                   "$EXIT_STATUS are not set, and TimeoutStopSec does not bound it")

    # ---- timeouts ----
    dag_timeout = None
    if timeout_start is not None and timeout_start[0] != math.inf:
        span, d = timeout_start
        if stype == "oneshot":
            dag_timeout = span
        else:
            secs = int(math.ceil(span))
            for s in steps:
                if s.name.startswith("exec-start-pre"):
                    s.timeout = secs
            if execs["ExecStartPre"]:
                report.add(APPROXIMATED, d.where,
                           "for Type=%s the start timeout bounds only the start-up phase: each "
                           "ExecStartPre step got timeout_sec=%d instead of one shared budget"
                           % (stype, secs))
            else:
                report.add(IGNORED, d.where,
                           "for Type=%s the start timeout does not bound the main process, "
                           "so there is nothing to translate" % stype)
    if runtime_max is not None and runtime_max[0] != math.inf:
        span, d = runtime_max
        dag_timeout = span if dag_timeout is None else dag_timeout + span
    if dag_timeout is not None and random_delay > 0:
        dag_timeout += math.ceil(random_delay)
        report.add(APPROXIMATED, "%s RandomizedDelaySec=" % timer_name,
                   "timeout_sec includes the %ds maximum random delay, which systemd does not "
                   "count" % int(math.ceil(random_delay)))

    # ---- environment ----
    env_text = None
    dotenv = []
    env_inline = None
    if env_assign:
        merged = {}
        for key, word, _ in env_assign:
            merged.pop(key, None)
            merged[key] = word
        if inline_env:
            env_inline = []
            for key, word in merged.items():
                env_inline.append({key: plain_text(word)})
            report.add(APPROXIMATED, env_assign[0][2],
                       "--inline-env: values are copied into the DAG; Dagu expands ${...} in "
                       "them, systemd does not")
        else:
            body = ["# Environment= from %s, written by timer2dagu" % service_name]
            body += ["%s=%s" % (k, dotenv_value(w)) for k, w in merged.items()]
            env_text = "\n".join(body) + "\n"
            dotenv.append(env_file)
    for path, optional, where in env_files:
        dotenv.append(path)
        if optional:
            report.add(APPROXIMATED, where,
                       "'-' (optional file) dropped: check how your Dagu version treats a "
                       "missing dotenv file")
    if env_files and env_assign and inline_env:
        report.add(APPROXIMATED, env_files[0][2],
                   "with --inline-env, Dagu's precedence between env and dotenv decides "
                   "conflicts; systemd lets EnvironmentFile= win")
    if dotenv:
        report.add(IGNORED, "%s" % service_name,
                   "Dagu also loads a .env file from the working directory if one exists; "
                   "systemd would not")

    # ---- persistent ----
    if persistent:
        if schedules:
            report.add(APPROXIMATED, "%s Persistent=" % timer_name,
                       "catch-up is limited to a %s window (systemd has no limit) and Dagu "
                       "may queue more than one missed run; overlap_policy=latest keeps the "
                       "newest" % catchup_window)
        else:
            report.add(IGNORED, "%s Persistent=" % timer_name, "no calendar schedule to catch up")
    if not schedules:
        report.add(UNTRANSLATED, timer_name,
                   "no schedule could be translated: the DAG runs only when started by hand")

    # ---- assemble ----
    name = re.sub(r"[^A-Za-z0-9_.-]", "-", stem)
    if name != stem:
        report.add(APPROXIMATED, timer_name,
                   "DAG name %r became %r: Dagu allows only letters, digits, '_', '.' and '-'"
                   % (stem, name))
    if len(name) > 40:
        report.add(APPROXIMATED, timer_name,
                   "DAG name %r is longer than the 40 characters Dagu accepts; rename the "
                   "DAG file and set name" % name)
    doc = {"name": name}
    desc = descriptions.get("service") or descriptions.get("timer")
    if desc:
        doc["description"] = desc
    if len(schedules) == 1:
        doc["schedule"] = schedules[0]
    elif schedules:
        doc["schedule"] = schedules
    if persistent and schedules:
        doc["catchup_window"] = catchup_window
        doc["overlap_policy"] = "latest"
    if working_dir:
        doc["working_dir"] = working_dir
    if dotenv:
        doc["dotenv"] = dotenv
    if env_inline:
        doc["env"] = env_inline
    if dag_timeout is not None:
        doc["timeout_sec"] = int(math.ceil(dag_timeout))
    doc["steps"] = []
    prev = None
    for s in steps:
        item = {"name": s.name, "description": s.origin, "run": s.run}
        if prev:
            item["depends"] = prev
        if s.timeout:
            item["timeout_sec"] = s.timeout
        if s.continue_on_failure:
            item["continue_on"] = {"failure": True, "mark_success": True}
        doc["steps"].append(item)
        prev = s.name
    if handler is not None:
        doc["handler_on"] = {"exit": {"run": handler}}

    title = "%s + %s%s" % (timer_name, service_name, " (user unit)" if user else "")
    header = "# Generated by timer2dagu %s from %s and %s.\n" % (
        __version__, timer_name, service_name)
    return (header + to_yaml(doc), env_text,
            report.render(title, (timer_name, service_name)), report)


def _go_hms(seconds):
    s = int(math.ceil(seconds))
    out = ""
    for unit, size in (("h", 3600), ("m", 60), ("s", 1)):
        if s >= size:
            out += "%d%s" % (s // size, unit)
            s %= size
    return out


def _timer_value(directives, key):
    for d in reversed(directives):
        if d.key == key:
            return d.value
    return ""


def _bool(d):
    v = d.value.lower()
    if v in ("1", "yes", "y", "true", "t", "on"):
        return True
    if v in ("0", "no", "n", "false", "f", "off"):
        return False
    raise ConvertError("%s not a boolean: %r" % (d.where, d.value))


def _split_assignment(word):
    """(key, value word) for a KEY=value word, or None if it is not one."""
    if not word or word[0][0] != "lit" or "=" not in word[0][1]:
        return None
    key, rest = word[0][1].split("=", 1)
    if not re.match("^" + _VARNAME + "$", key):
        return None
    tail = ([("lit", rest)] if rest else []) + list(word[1:])
    return key, tail or [("lit", "")]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None):
    p = argparse.ArgumentParser(
        prog="timer2dagu",
        description="Convert a systemd .timer + .service pair into a Dagu DAG.")
    p.add_argument("timer", help="the .timer unit file")
    p.add_argument("service", nargs="?",
                   help="the .service unit file (default: the timer's Unit=, or the "
                        "same name, next to the timer)")
    p.add_argument("-o", "--output", help="write the DAG here instead of stdout")
    p.add_argument("--report", help="write the report here instead of stderr")
    p.add_argument("--env-out",
                   help="companion .env file for Environment= values (default: next to "
                        "--output, named <dag>.env)")
    p.add_argument("--inline-env", action="store_true",
                   help="copy Environment= values into the DAG's env instead")
    p.add_argument("--user", action="store_true",
                   help="treat the units as user units (implied by a .../systemd/user/ path)")
    p.add_argument("--home", help="home directory to substitute for %%h and ~ in user units")
    p.add_argument("--user-name", help="user name to substitute for %%u in user units")
    p.add_argument("--catchup-window", default="7d",
                   help="Dagu catchup_window for Persistent=true (default: 7d)")
    p.add_argument("--strict", action="store_true",
                   help="exit 2 if anything was left untranslated")
    p.add_argument("--version", action="version", version="%(prog)s " + __version__)
    args = p.parse_args(argv)

    try:
        timer_text = _read(args.timer)
        timer_name = os.path.basename(args.timer)
        if args.service:
            service_path = args.service
        else:
            target = None
            for d in parse_unit(timer_text, timer_name):
                if d.section == "Timer" and d.key == "Unit":
                    target = d.value
            service_path = os.path.join(os.path.dirname(args.timer),
                                        target or timer_name.rsplit(".", 1)[0] + ".service")
        service_text = _read(service_path)
        user = args.user or _is_user_path(os.path.abspath(args.timer))
        dag_stem = timer_name.rsplit(".", 1)[0]
        env_out = args.env_out
        if env_out is None and args.output:
            env_out = os.path.join(os.path.dirname(args.output),
                                   os.path.basename(args.output).rsplit(".", 1)[0] + ".env")
        # Absolute: Dagu looks for a relative dotenv path in working_dir first.
        env_ref = os.path.abspath(env_out) if env_out else None
        yaml_text, env_text, report_text, report = convert(
            timer_text, timer_name, service_text, os.path.basename(service_path),
            user=user, home=args.home, user_name=args.user_name,
            catchup_window=args.catchup_window, env_file=env_ref or (dag_stem + ".env"),
            inline_env=args.inline_env)
        if env_text is not None and not env_out:
            raise ConvertError("the service sets Environment=; pass -o or --env-out for the "
                               "companion .env file, or --inline-env")
    except (ConvertError, OSError) as e:
        print("timer2dagu: %s" % e, file=sys.stderr)
        return 1

    if args.output:
        _write(args.output, yaml_text)
    else:
        sys.stdout.write(yaml_text)
    if env_text is not None:
        _write(env_out, env_text, private=True)
    if args.report:
        _write(args.report, report_text)
    else:
        sys.stderr.write(report_text)
    if args.strict and report.has(UNTRANSLATED):
        return 2
    return 0


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text, private=False):
    if private:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


if __name__ == "__main__":
    sys.exit(main())
