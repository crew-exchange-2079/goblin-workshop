"""OFFLINE-ONLY parser for already-captured `tapeinfo` text.

This is not part of the exporter. Nothing in `lto_exporter` imports it, and
it never runs a command, opens a device or emits Prometheus metrics. It is
here only so captured or synthetic text can be inspected.

`tapeinfo` itself is deliberately not run by this project: it opens the
device O_RDWR and reads the TapeAlert log page (0x2E), which on some drives
and TapeAlert modes clears the flags.

Line formats are from tapeinfo.c in mtx 1.3.12
(https://sourceforge.net/projects/mtx/, man page tapeinfo(1)):
"Vendor ID: '...'", "Product ID: '...'", "Ready: yes|no",
"DataCompEnabled: yes|no", "TapeAlert[N]: <message>." (printed only for
flags that are set, so silence does NOT mean TapeAlert is supported).

Usage:  python3 -m offline.tapeinfo_text < captured.txt
"""

import json
import re
import sys

_KV_RE = re.compile(r"^([A-Za-z][A-Za-z ]{0,40}):\s*(.*?)\s*$")
_TA_RE = re.compile(r"^TapeAlert\[([0-9]{1,3})\]:")
_SAFE_STR = re.compile(r"[^A-Za-z0-9 ._-]")


def _clean(val):
    val = _SAFE_STR.sub("", val.strip().strip("'")).strip()
    return val[:32] or None


def _yesno(val):
    return {"yes": True, "no": False}.get(val.strip().lower())


def parse_tapeinfo_text(text):
    """Return a dict. Unknown or missing fields are None, never 0/False.

    `tapealert_codes_listed` holds only codes that appear in the text. There
    is deliberately no count or "no alerts" field, because an empty list
    can't distinguish "none set" from "unsupported" or "not read".
    """
    out = {"parsed": False, "vendor": None, "product": None, "ready": None,
           "compression_enabled": None, "tapealert_codes_listed": [],
           "tapealert_support": "unknown"}
    codes = set()
    for line in text.splitlines():
        m = _TA_RE.match(line)
        if m:
            code = int(m.group(1))
            if 1 <= code <= 64:
                codes.add(code)
                out["parsed"] = True
            continue
        m = _KV_RE.match(line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        # Deliberately never parsed: SerialNumber, SCSI ID/LUN, Block Position.
        field = {"Vendor ID": "vendor", "Product ID": "product", "Ready": "ready",
                 "DataCompEnabled": "compression_enabled"}.get(key)
        if field is None or out[field] is not None:
            continue
        out[field] = _clean(val) if field in ("vendor", "product") else _yesno(val)
        out["parsed"] = True
    out["tapealert_codes_listed"] = sorted(codes)
    return out


if __name__ == "__main__":
    print(json.dumps(parse_tapeinfo_text(sys.stdin.read()), indent=2))
