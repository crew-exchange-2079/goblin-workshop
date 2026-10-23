"""Cron parsing that mirrors Dagu's scheduler.

Dagu (internal/ir/schedule.go) parses schedules with robfig/cron v3 using
exactly five fields plus descriptors, accepts a leading `CRON_TZ=` or `TZ=`
field, and handles `@every <duration>` itself on a grid aligned to the Unix
epoch (whole minutes only, no timezone prefix). This module re-implements
those rules rather than using a Python cron library, whose dialects differ
(seconds fields, `L`, `#`, Sunday as 7).

Known approximation: robfig's handling of DST gaps and repeated hours is not
reproduced exactly. A local time that does not exist is skipped and a repeated
local time fires once (first occurrence).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt.timezone.utc
SEARCH_YEARS = 5  # robfig gives up after five years too

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
DOWS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
DOW_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]

DESCRIPTORS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    pass


@dataclasses.dataclass(frozen=True)
class Field:
    name: str
    lo: int
    hi: int
    names: tuple = ()


FIELDS = (
    Field("minute", 0, 59),
    Field("hour", 0, 23),
    Field("day-of-month", 1, 31),
    Field("month", 1, 12, tuple(MONTHS)),
    Field("day-of-week", 0, 6, tuple(DOWS)),
)


def _value(text: str, f: Field) -> int:
    low = text.lower()
    if f.names and low in f.names:
        return f.names.index(low) + f.lo
    if not text.isdigit():
        raise CronError(f"{f.name}: {text!r} is not a number" + (" or name" if f.names else ""))
    return int(text)


def _parse_field(text: str, f: Field) -> tuple[frozenset, bool]:
    """Return (values, star). `star` follows robfig's starBit: set for a bare
    `*`/`?` (or `*/1`), cleared by a step greater than one."""
    values: set = set()
    star = False
    for part in text.split(","):
        if not part:
            raise CronError(f"{f.name}: empty list element in {text!r}")
        rng, _, step_s = part.partition("/")
        if _ and not step_s:
            raise CronError(f"{f.name}: missing step in {part!r}")
        part_star = False
        if rng in ("*", "?"):
            lo, hi, part_star = f.lo, f.hi, True
        else:
            a, dash, b = rng.partition("-")
            lo = _value(a, f)
            hi = _value(b, f) if dash else lo
        step = 1
        if step_s:
            if not step_s.isdigit():
                raise CronError(f"{f.name}: step {step_s!r} is not a number")
            step = int(step_s)
            if step == 0:
                raise CronError(f"{f.name}: step must be positive in {part!r}")
            if "-" not in rng and not part_star:
                hi = f.hi  # robfig: "N/step" means "N-max/step"
            if step > 1:
                part_star = False
        if lo < f.lo:
            raise CronError(f"{f.name}: {lo} is below the minimum {f.lo}")
        if hi > f.hi:
            raise CronError(f"{f.name}: {hi} is above the maximum {f.hi}")
        if lo > hi:
            raise CronError(f"{f.name}: range {part!r} starts after it ends")
        values.update(range(lo, hi + 1, step))
        star = star or part_star
    return frozenset(values), star


_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h)")
_UNIT_SEC = {"ns": 1e-9, "us": 1e-6, "µs": 1e-6, "ms": 1e-3, "s": 1, "m": 60, "h": 3600}


def parse_go_duration(text: str) -> float:
    """Seconds in a Go time.ParseDuration string (no sign support needed here)."""
    if text == "0":
        return 0.0
    pos, total = 0, 0.0
    for m in _DURATION_RE.finditer(text):
        if m.start() != pos:
            break
        total += float(m.group(1)) * _UNIT_SEC[m.group(2)]
        pos = m.end()
    if pos != len(text) or not text:
        raise CronError(f"invalid duration {text!r}")
    return total


@dataclasses.dataclass
class Schedule:
    expression: str  # whitespace-normalised source text
    tz: dt.tzinfo
    tz_name: str
    every_sec: int | None = None
    minutes: frozenset = frozenset()
    hours: frozenset = frozenset()
    doms: frozenset = frozenset()
    months: frozenset = frozenset()
    dows: frozenset = frozenset()
    dom_star: bool = True
    dow_star: bool = True

    def _day_matches(self, d: dt.date) -> bool:
        if d.month not in self.months:
            return False
        dom = d.day in self.doms
        dow = (d.isoweekday() % 7) in self.dows
        if self.dom_star or self.dow_star:
            return dom and dow
        return dom or dow

    def next_runs(self, after: dt.datetime, count: int) -> list[dt.datetime]:
        """The next `count` fire times strictly after `after` (aware datetime)."""
        out: list[dt.datetime] = []
        if count <= 0:
            return out
        if self.every_sec:
            t = int(after.timestamp()) // self.every_sec * self.every_sec
            for _ in range(count):
                t += self.every_sec
                out.append(dt.datetime.fromtimestamp(t, self.tz))
            return out
        local = after.astimezone(self.tz)
        day = local.date()
        end = day + dt.timedelta(days=366 * SEARCH_YEARS)
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        while day <= end and len(out) < count:
            if self._day_matches(day):
                for h in hours:
                    for m in minutes:
                        cand = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=self.tz)
                        # Skip local times that fall into a DST gap.
                        back = cand.astimezone(UTC).astimezone(self.tz)
                        if (back.hour, back.minute) != (h, m):
                            continue
                        if cand > after:
                            out.append(cand)
                            if len(out) == count:
                                return out
            day += dt.timedelta(days=1)
        return out

    def describe(self) -> str:
        if self.every_sec:
            return f"every {_plural(self.every_sec // 60, 'minute')} (on a grid aligned to the Unix epoch)"
        parts = [_describe_time(self.minutes, self.hours)]
        days = _describe_days(self)
        if days:
            parts.append(days)
        if self.months != frozenset(range(1, 13)):
            names = [MONTH_NAMES[a - 1] if a == b else f"{MONTH_NAMES[a - 1]}–{MONTH_NAMES[b - 1]}"
                     for a, b in _ranges(sorted(self.months))]
            parts.append("in " + _join(names))
        tz = f" ({self.tz_name})"
        return ", ".join(parts) + tz


def resolve_tz(name: str) -> dt.tzinfo:
    if name in ("UTC", "Etc/UTC"):
        return UTC
    if name == "Local":
        raise CronError("timezone 'Local' depends on the Dagu host; pass --tz to lint it")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise CronError(f"unknown timezone {name!r}") from e


def parse(expr: str, default_tz: str = "UTC") -> Schedule:
    if not isinstance(expr, str):
        raise CronError(f"expected a string, got {type(expr).__name__}")
    norm = " ".join(expr.split())
    if not norm:
        raise CronError("cron expression must not be empty")
    tz_name = default_tz
    body = norm
    explicit_tz = False
    if norm.startswith(("TZ=", "CRON_TZ=")) and " " in norm:
        prefix, body = norm.split(" ", 1)
        tz_name = prefix.split("=", 1)[1]
        explicit_tz = True
    tz = resolve_tz(tz_name)
    if body.startswith("@every "):
        if explicit_tz:
            raise CronError("@every does not accept a timezone prefix")
        sec = parse_go_duration(body[len("@every "):].strip())
        if sec < 60 or sec % 60:
            raise CronError("@every interval must be a positive whole number of minutes")
        return Schedule(norm, tz, tz_name, every_sec=int(sec))
    if body.startswith("@"):
        if body not in DESCRIPTORS:
            raise CronError(f"unrecognized descriptor {body!r}")
        body = DESCRIPTORS[body]
    fields = body.split(" ")
    if len(fields) != 5:
        raise CronError(f"expected exactly 5 fields (minute hour day-of-month month day-of-week), got {len(fields)}")
    parsed = [_parse_field(text, f) for text, f in zip(fields, FIELDS)]
    return Schedule(
        norm, tz, tz_name,
        minutes=parsed[0][0], hours=parsed[1][0], doms=parsed[2][0],
        months=parsed[3][0], dows=parsed[4][0],
        dom_star=parsed[2][1], dow_star=parsed[4][1],
    )


def day_fields_ored(s: Schedule) -> bool:
    """True when both day fields are restricted, so robfig fires on days that
    match EITHER of them (a frequent surprise: `0 9 1-7 * MON` is not "first Monday")."""
    return not s.every_sec and not s.dom_star and not s.dow_star


def misleading_steps(expr: str) -> list[str]:
    """Dagu's checkMisleadingStepValues: `*/N` on minute or hour where N does
    not divide the base resets at the top of the hour/day instead of every N."""
    fields = expr.split()
    if len(fields) == 6 and fields[0].startswith(("TZ=", "CRON_TZ=")):
        fields = fields[1:]
    if len(fields) != 5:
        return []
    out = []
    for idx, name, base in ((0, "minute", 60), (1, "hour", 24)):
        f = fields[idx]
        if not f.startswith("*/") or not f[2:].isdigit():
            continue
        step = int(f[2:])
        if step <= 1 or base % step == 0:
            continue
        fires = list(range(0, base, step))
        out.append(
            f"{name} field {f!r} fires at {', '.join(map(str, fires))}, then resets at the top of the "
            f"{'hour' if name == 'minute' else 'day'}; it is not every {step} {name}s"
        )
    return out


# --- human-readable rendering ---------------------------------------------

def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}" if n == 1 else f"{n} {unit}s"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _progression(vals: list[int], lo: int, hi: int):
    """(start, step) when vals is start, start+step, ... up to hi; else None."""
    if len(vals) < 3:
        return None
    step = vals[1] - vals[0]
    if step < 2 or any(b - a != step for a, b in zip(vals, vals[1:])):
        return None
    if vals[-1] + step <= hi or (hi + 1 - lo) % step:
        return None  # an uneven step resets at the wrap, so it is not "every N"
    return vals[0], step


def _ranges(vals: list[int]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for v in vals:
        if out and v == out[-1][1] + 1:
            out[-1][1] = v
        else:
            out.append([v, v])
    return [(a, b) for a, b in out]


def _describe_time(minutes: frozenset, hours: frozenset) -> str:
    mins, hrs = sorted(minutes), sorted(hours)
    all_min, all_hr = len(mins) == 60, len(hrs) == 24
    if len(mins) * len(hrs) <= 6:
        times = [f"{h:02d}:{m:02d}" for h in hrs for m in mins]
        return "at " + _join(times)
    if all_min:
        m_txt = "every minute"
    elif (p := _progression(mins, 0, 59)) and p[0] == 0:
        m_txt = f"every {p[1]} minutes"
    elif len(mins) == 1:
        m_txt = f"at minute {mins[0]}"
    else:
        m_txt = "at minutes " + _join(_fmt_ranges(mins, "{}"))
    if all_hr:
        return m_txt + (" of every hour" if len(mins) == 1 else "")
    if (p := _progression(hrs, 0, 23)) and p[0] == 0:
        return f"{m_txt}, every {p[1]} hours"
    return f"{m_txt}, during hour(s) " + _join(_fmt_ranges(hrs, "{:02d}"))


def _fmt_ranges(vals: list[int], fmt: str) -> list[str]:
    out = []
    for a, b in _ranges(vals):
        out.append(fmt.format(a) if a == b else f"{fmt.format(a)}–{fmt.format(b)}")
    return out


def _describe_days(s: Schedule) -> str:
    full_dom, full_dow = s.doms == frozenset(range(1, 32)), s.dows == frozenset(range(7))
    if not (s.dom_star or s.dow_star) and (full_dom or full_dow):
        return "every day"  # robfig ORs the two day fields when neither is `*`
    dom_txt = dow_txt = ""
    if not full_dom:
        dom_txt = "on day " + _join(_fmt_ranges(sorted(s.doms), "{}")) + " of the month"
    if not full_dow:
        names = []
        for a, b in _ranges(sorted(s.dows)):
            names.append(DOW_NAMES[a] if a == b else f"{DOW_NAMES[a]}–{DOW_NAMES[b]}")
        dow_txt = "on " + _join(names)
    if dom_txt and dow_txt:
        return dom_txt + " or " + dow_txt
    if dom_txt or dow_txt:
        return dom_txt or dow_txt
    return "every day"
