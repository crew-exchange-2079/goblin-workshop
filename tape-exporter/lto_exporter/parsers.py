"""Strict-header, tolerant-body parser for sg_logs error-counter page text.

Sources for every parsed field (public; checked against sg3_utils git
1929463554c5dc749c5a89bcefb2c88a08d45575 and the Ubuntu 24.04 sg3-utils
1.46 binary):
  * sg_logs(8), sg3_utils: https://sg.danny.cz/sg/sg_logs.html and
    https://manpages.debian.org/stable/sg3-utils/sg_logs.8.en.html
    The text layout comes from show_error_counter_page() in
    https://github.com/doug-gilbert/sg3_utils/blob/main/src/sg_logs.c :
    header "<Write|Read> error counter log page  [0xN]" (sg3_utils 1.46
    prints "... error counter page  [0xN]", without "log"), then one line
    per parameter "  <name> = <decimal>[ [N GB|TB]]".
    Parameter codes 0x0-0x6 of the Write (0x2) and Read (0x3) Error Counter
    log pages are defined by T10 SPC ("Error counter log pages").
  * Exit status, include/sg_lib.h: 0 ok, 1 syntax error, 2 not ready,
    5 illegal request, 6 unit attention, 9 invalid opcode, 12/13 not ready (standby /
    unavailable), 15 file error, 33 command timeout, 99 other.
    An open() failure is NOT 15: sg_logs.c returns sg_convert_errno(errno)
    (lib/sg_lib.c), i.e. 50 + errno for errno < 47, else 50. For example a
    missing node exits 52 (ENOENT) and a permission problem 63 (EACCES).

Fail closed: output is only accepted if it contains the header of the
expected page (direction *and* page code). Headerless output, or output
whose header names another page (e.g. "Verify error counter page [0x5]",
"Read Reverse error counter page [0x4]"), is a parse_error. Lines under the
expected header that aren't a known parameter are ignored. A value that is
absent or malformed is reported as missing, never as zero.
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
PAGE_CODES = {"write": 0x2, "read": 0x3}

# Any page header: starts at column 0 and ends with "[0xN]" (optional ":").
_ANY_HEADER_RE = re.compile(r"(\S.*?)\s+\[0x([0-9a-fA-F]{1,2})\]:?\s*")
_EC_HEADER_RE = re.compile(r"(Write|Read) error counter(?: log)? page", re.I)
_PARAM_RE = re.compile(r"\s+(.+?)\s*=\s*([0-9]{1,20})(?:\s+\[[^\]\n]*\])?\s*")
_U64_MAX = 2**64 - 1


def _sg_exit_status():
    m = {0: "ok", 1: "syntax_error", 2: "not_ready", 6: "unit_attention", 12: "not_ready",
         13: "not_ready", 5: "unsupported", 9: "unsupported",
         15: "file_error", 33: "device_timeout"}
    for rc in range(50, 97):
        m[rc] = "os_error"
    return m


SG_EXIT_STATUS = _sg_exit_status()


def sg_exit_status(returncode):
    return SG_EXIT_STATUS.get(returncode, "error")


@dataclass
class ErrorCounterPage:
    status: str                       # ok | parse_error
    values: dict = field(default_factory=dict)   # label -> int (present only)


def _is_expected_header(title, code, expected_direction):
    m = _EC_HEADER_RE.fullmatch(title)
    return (m is not None and m.group(1).lower() == expected_direction
            and int(code, 16) == PAGE_CODES[expected_direction])


def parse_sg_logs_error_counters(text, expected_direction):
    if expected_direction not in PAGE_CODES:
        raise ValueError(f"unknown direction {expected_direction!r}")
    values = {}
    seen = set()
    header_seen = False
    in_expected = False
    for line in text.splitlines():
        h = _ANY_HEADER_RE.fullmatch(line)
        if h:
            in_expected = _is_expected_header(h.group(1), h.group(2), expected_direction)
            header_seen = header_seen or in_expected
            continue
        if not in_expected:
            continue
        m = _PARAM_RE.fullmatch(line)
        if not m:
            continue
        label = _BY_TEXT.get(m.group(1).lower())
        if label is None or label in seen:
            continue
        seen.add(label)
        val = int(m.group(2))
        if val <= _U64_MAX:
            values[label] = val
    if not header_seen:
        return ErrorCounterPage("parse_error")
    return ErrorCounterPage("ok", values)
