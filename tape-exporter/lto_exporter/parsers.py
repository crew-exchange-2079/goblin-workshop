"""Tolerant parser for sg_logs error-counter page text output.

Sources for every parsed field (public):
  * sg_logs(8), sg3_utils: https://sg.danny.cz/sg/sg_logs.html and
    https://manpages.debian.org/stable/sg3-utils/sg_logs.8.en.html
    The text layout comes from show_error_counter_page() in
    https://github.com/doug-gilbert/sg3_utils/blob/main/src/sg_logs.c :
    header "<Write|Read> error counter log page  [0xN]" then one line per
    parameter "  <name> = <decimal>[ [N GB|TB]]".
    Parameter codes 0x0-0x6 of the Write (0x2) and Read (0x3) Error Counter
    log pages are defined by T10 SPC ("Error counter log pages").
  * sg3_utils(8) EXIT STATUS / include/sg_lib.h: 0 ok, 2 not ready,
    5 illegal request, 9 invalid opcode, 15 file error, 33 timeout.

Anything not recognised is ignored. A value that is absent or malformed is
reported as missing, never as zero.
"""

import re
from dataclasses import dataclass, field

# code -> (sg_logs text, metric parameter label)
ERROR_COUNTER_PARAMS = {
    0: ("Errors corrected without substantial delay", "corrected_without_delay"),
    1: ("Errors corrected with possible delays", "corrected_with_delay"),
    2: ("Total rewrites or rereads", "rewrites_or_rereads"),
    3: ("Total errors corrected", "total_corrected"),
    4: ("Total times correction algorithm processed", "correction_algorithm_processed"),
    5: ("Total bytes processed", "bytes_processed"),
    6: ("Total uncorrected errors", "total_uncorrected"),
}
_BY_TEXT = {text.lower(): label for text, label in ERROR_COUNTER_PARAMS.values()}
PARAM_LABELS = tuple(label for _, label in ERROR_COUNTER_PARAMS.values())

_HEADER_RE = re.compile(r"^\s*(write|read) error counter(?: log)? page\b", re.I)
_PARAM_RE = re.compile(r"^\s+(.+?)\s*=\s*([0-9]{1,20})(?:\s+\[[^\]]*\])?\s*$")
_U64_MAX = 2**64 - 1

SG_EXIT_STATUS = {0: "ok", 2: "not_ready", 5: "unsupported", 9: "unsupported",
                  15: "open_failed", 33: "device_timeout"}


@dataclass
class ErrorCounterPage:
    status: str                       # ok | parse_error
    values: dict = field(default_factory=dict)   # label -> int (present only)


def parse_sg_logs_error_counters(text, expected_direction):
    values = {}
    header_seen = False
    other_header = False
    for line in text.splitlines():
        m = _HEADER_RE.match(line)
        if m:
            other_header = m.group(1).lower() != expected_direction
            header_seen = header_seen or not other_header
            continue
        if other_header:
            continue
        m = _PARAM_RE.match(line)
        if not m:
            continue
        label = _BY_TEXT.get(m.group(1).lower())
        if label is None or label in values:
            continue
        val = int(m.group(2))
        if val <= _U64_MAX:
            values[label] = val
    if not header_seen and not values:
        return ErrorCounterPage("parse_error")
    return ErrorCounterPage("ok", values)
