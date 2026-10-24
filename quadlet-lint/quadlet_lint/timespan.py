"""Parse systemd time spans (systemd.time(7)) into seconds."""

import re

UNITS = {
    "us": 1e-6, "usec": 1e-6, "µs": 1e-6,
    "ms": 1e-3, "msec": 1e-3,
    "s": 1, "sec": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "week": 604800, "weeks": 604800,
    "M": 2629800, "month": 2629800, "months": 2629800,
    "y": 31557600, "year": 31557600, "years": 31557600,
}
PART_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-zµ]*)")

INFINITY = float("inf")


def parse_timespan(text, default_unit=1):
    """Seconds as a float, INFINITY for "infinity", or None if unparseable.

    A bare number uses default_unit (seconds for TimeoutStopSec=).
    """
    s = text.strip()
    if s == "infinity":
        return INFINITY
    if not s:
        return None
    total = 0.0
    pos = 0
    for m in PART_RE.finditer(s):
        if s[pos:m.start()].strip():
            return None
        number, unit = m.groups()
        if unit:
            if unit not in UNITS:
                return None
            total += float(number) * UNITS[unit]
        else:
            total += float(number) * default_unit
        pos = m.end()
    if pos == 0 or s[pos:].strip():
        return None
    return total


def format_seconds(value):
    if value == INFINITY:
        return "infinity"
    if value == int(value):
        return f"{int(value)}s"
    return f"{value:g}s"
