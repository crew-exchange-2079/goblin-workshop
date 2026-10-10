"""Tolerant parsers for sg_logs and tapeinfo text output.

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
  * tapeinfo(1), mtx: https://manpages.debian.org/stable/mtx/tapeinfo.1.en.html
    Line formats come from tapeinfo.c in mtx 1.3.12
    (https://sourceforge.net/projects/mtx/): "Vendor ID: '...'",
    "Product ID: '...'", "Ready: yes|no", "DataCompEnabled: yes|no",
    "TapeAlert[N]: <message>." (printed only for flags that are set).

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


@dataclass
class TapeInfo:
    status: str                       # ok | parse_error
    vendor: str | None = None
    product: str | None = None
    ready: int | None = None
    compression_enabled: int | None = None
    tapealert_flags: tuple = ()


_KV_RE = re.compile(r"^([A-Za-z][A-Za-z ]{0,40}):\s*(.*?)\s*$")
_TA_RE = re.compile(r"^TapeAlert\[([0-9]{1,3})\]:")
_SAFE_STR = re.compile(r"[^A-Za-z0-9 ._-]")


def _clean(val):
    val = _SAFE_STR.sub("", val.strip().strip("'")).strip()
    return val[:32] or None


def _yesno(val):
    return {"yes": 1, "no": 0}.get(val.strip().lower())


def parse_tapeinfo(text):
    info = TapeInfo("ok")
    flags = set()
    recognised = False
    for line in text.splitlines():
        m = _TA_RE.match(line)
        if m:
            code = int(m.group(1))
            if 1 <= code <= 64:
                flags.add(code)
                recognised = True
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # Deliberately not parsed: SerialNumber, SCSI ID/LUN, Block Position.
        if key == "Vendor ID" and info.vendor is None:
            info.vendor = _clean(val)
        elif key == "Product ID" and info.product is None:
            info.product = _clean(val)
        elif key == "Ready" and info.ready is None:
            info.ready = _yesno(val)
        elif key == "DataCompEnabled" and info.compression_enabled is None:
            info.compression_enabled = _yesno(val)
        else:
            continue
        recognised = True
    if not recognised:
        return TapeInfo("parse_error")
    info.tapealert_flags = tuple(sorted(flags))
    return info
