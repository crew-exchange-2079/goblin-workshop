#!/usr/bin/env python3
"""tapelabel: LTO cartridge barcode labels as print-ready PDF sheets.

Python 3 standard library only. One label is eight Code 39 characters: a
six-character volser (A-Z, 0-9) followed by a two-character media ID (L5..L9,
WORM LV..LZ, cleaning CU), with no check character and '*' start/stop.

    tapelabel.py validate TAP001L9 CLN001CU
    tapelabel.py pdf --first TAP000 --count 30 --media L9 -o labels.pdf
    tapelabel.py stocks
"""

import argparse
import json
import math
import re
import sys
import zlib

# --- symbology -------------------------------------------------------------

# Code 39 element widths, bar/space alternating starting with a bar:
# n = narrow, w = wide. Exactly three of the nine elements are wide. Only the
# characters an LTO label can hold (A-Z, 0-9) plus the '*' start/stop.
CODE39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn",
    "4": "nnnwwnnnw", "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw",
    "8": "wnnwnnwnn", "9": "nnwwnnwnn", "A": "wnnnnwnnw", "B": "nnwnnwnnw",
    "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn", "F": "nnwnwwnnn",
    "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww",
    "O": "wnnnwnnwn", "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn",
    "S": "nnwnnnwwn", "T": "nnnnwnwwn", "U": "wwnnnnnnw", "V": "nwwnnnnnw",
    "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn", "Z": "nwwnwnnnn",
    "*": "nwnnwnwnn",
}

# Nominal symbol geometry, mm. X (narrow element) 0.423 mm and wide:narrow
# 2.75:1 are the values IBM's library documentation gives for LTO labels;
# the inter-character gap is one X. Quiet zone minimum is the USS-39 10X.
NARROW_MM = 0.423
RATIO = 2.75
GAP_MM = NARROW_MM
QUIET_MIN_MM = 10 * NARROW_MM
BAR_MIN_MM = 11.1

# Media ID -> description. LTO-5..9 data, LTO-5..9 WORM, universal cleaning.
MEDIA = {
    "L5": "LTO-5", "L6": "LTO-6", "L7": "LTO-7", "L8": "LTO-8", "L9": "LTO-9",
    "LV": "LTO-5 WORM", "LW": "LTO-6 WORM", "LX": "LTO-7 WORM",
    "LY": "LTO-8 WORM", "LZ": "LTO-9 WORM",
    "CU": "universal cleaning",
}

VOLSER_RE = re.compile(r"[A-Z0-9]{6}")

# Code 39 mod-43 values (0-9, then A-Z; the symbols beyond Z cannot occur here).
MOD43 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ-. $/+%"


def mod43(text):
    return MOD43[sum(MOD43.index(c) for c in text) % 43]


def looks_checked(label):
    """True if the last character equals the mod-43 check of the first seven.

    LTO labels carry no check character, but about 1 in 43 look as if they
    do, and a reader set to validate-and-strip Code 39 check characters
    would report only the first seven. Not an error: a reader setting to know.
    """
    return mod43(label[:7]) == label[7]


def modules(text):
    """Element widths in X units for '*text*', gaps included, bar first."""
    out = []
    for i, ch in enumerate("*" + text + "*"):
        if ch not in CODE39:
            raise ValueError(f"character {ch!r} not encodable")
        if i:
            out.append(GAP_MM / NARROW_MM)  # gap is a space
        out.extend(RATIO if e == "w" else 1.0 for e in CODE39[ch])
    return out


def symbol_width_mm(text):
    return sum(modules(text)) * NARROW_MM


def bars_mm(text):
    """(x, width) of each bar in mm from the first bar's left edge."""
    x, out = 0.0, []
    for i, w in enumerate(modules(text)):
        if i % 2 == 0:
            out.append((x, w * NARROW_MM))
        x += w * NARROW_MM
    return out


# --- validation ------------------------------------------------------------

class LabelError(ValueError):
    pass


def check(label, strict=False):
    """Validate an 8-character label. Return warnings; raise LabelError."""
    if len(label) != 8:
        raise LabelError(f"{label!r}: need 6-char volser + 2-char media ID, got {len(label)} chars")
    volser, media = label[:6], label[6:]
    if not VOLSER_RE.fullmatch(volser):
        hint = " (uppercase only)" if VOLSER_RE.fullmatch(volser.upper()) else ""
        raise LabelError(f"{label!r}: volser must be 6 of A-Z 0-9{hint}")
    if media not in MEDIA:
        raise LabelError(f"{label!r}: unknown media ID {media!r} (known: {', '.join(MEDIA)})")
    if media == "CU" and not volser.startswith("CLN"):
        raise LabelError(f"{label!r}: cleaning media ID CU needs a CLN-prefixed volser")
    if volser.startswith("CLN") and media != "CU":
        raise LabelError(f"{label!r}: CLN volsers are treated as cleaning cartridges; use CU")
    warns = []
    if set(volser) & set("O0") == {"O", "0"} or set(volser) & set("I1") == {"I", "1"}:
        warns.append(f"{label}: mixes O/0 or I/1, easy to misread by eye")
    if strict and warns:
        raise LabelError(warns[0])
    return warns


def volser_range(first, count):
    """count volsers from first, incrementing its trailing digit run."""
    m = re.fullmatch(r"([A-Z0-9]*?)([0-9]+)", first)
    if not VOLSER_RE.fullmatch(first) or not m:
        raise LabelError(f"{first!r}: --first must be a 6-char volser ending in digits")
    prefix, digits = m.groups()
    start = int(digits)
    last = start + count - 1
    if count < 1 or last >= 10 ** len(digits):
        raise LabelError(f"{first!r} + {count}: range overflows {len(digits)} digit(s)")
    return [f"{prefix}{n:0{len(digits)}d}" for n in range(start, last + 1)]


# --- sheet layouts ---------------------------------------------------------

# Generic geometry, not any vendor's product: measure your stock and pass
# --stock-json if it differs. All mm; left/top are the first label's corner.
STOCKS = {
    "letter-2x15": dict(page_w=215.9, page_h=279.4, cols=2, rows=15, left=24.95,
                        top=12.95, pitch_x=88.0, pitch_y=16.9, label_w=78.0, label_h=16.9),
    "a4-2x16": dict(page_w=210.0, page_h=297.0, cols=2, rows=16, left=22.0,
                    top=13.3, pitch_x=88.0, pitch_y=16.9, label_w=78.0, label_h=16.9),
    "single": dict(page_w=78.0, page_h=16.9, cols=1, rows=1, left=0.0,
                   top=0.0, pitch_x=78.0, pitch_y=16.9, label_w=78.0, label_h=16.9),
}

TEXT_BAND_MM = 4.2  # human-readable line above the bars
TEXT_SIZE_MM = 4.4  # Courier-Bold em; its capitals are about 2.5 mm tall
PAD_MM = 0.4


# PDF's default user space is limited to 14400 units (200 in) per page side.
MAX_MM = 14400 / 72 * 25.4


def check_stock(s):
    need = {"page_w", "page_h", "cols", "rows", "left", "top", "pitch_x", "pitch_y", "label_w", "label_h"}
    if set(s) != need:
        raise LabelError(f"stock needs exactly keys {sorted(need)}")
    # 0 <= v <= MAX_MM also refuses NaN, infinities and huge JSON integers, and
    # keeps every coordinate finite after the mm -> pt conversion.
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= MAX_MM
           for v in s.values()):
        raise LabelError(f"stock values must be finite numbers from 0 to {MAX_MM:g} mm")
    if s["cols"] < 1 or s["rows"] < 1 or s["cols"] != int(s["cols"]) or s["rows"] != int(s["rows"]):
        raise LabelError("stock cols/rows must be positive integers")
    right = s["left"] + (s["cols"] - 1) * s["pitch_x"] + s["label_w"]
    bottom = s["top"] + (s["rows"] - 1) * s["pitch_y"] + s["label_h"]
    if right > s["page_w"] + 1e-6 or bottom > s["page_h"] + 1e-6:
        raise LabelError("stock labels run off the page")
    if s["pitch_x"] < s["label_w"] - 1e-6 and s["cols"] > 1 or s["pitch_y"] < s["label_h"] - 1e-6 and s["rows"] > 1:
        raise LabelError("stock labels overlap")
    sym = symbol_width_mm("A" * 8)
    if (s["label_w"] - sym) / 2 < QUIET_MIN_MM - 1e-9:
        raise LabelError(f"label_w {s['label_w']} mm leaves less than the {QUIET_MIN_MM:.2f} mm quiet zones")
    if s["label_h"] - TEXT_BAND_MM - 2 * PAD_MM < BAR_MIN_MM:
        raise LabelError(f"label_h {s['label_h']} mm leaves bars shorter than {BAR_MIN_MM} mm")
    return s


# --- PDF -------------------------------------------------------------------

PT = 72 / 25.4


def _f(v):
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def label_ops(label, x0, y0, s):
    """PDF content ops for one label with its top-left at (x0, y0) mm from page top-left."""
    ph = s["page_h"]
    w, h = s["label_w"], s["label_h"]
    quiet = (w - symbol_width_mm(label)) / 2
    bar_top = y0 + PAD_MM + TEXT_BAND_MM
    bar_h = h - TEXT_BAND_MM - 2 * PAD_MM
    ops = []
    for bx, bw in bars_mm(label):
        ops.append(f"{_f((x0 + quiet + bx) * PT)} {_f((ph - bar_top - bar_h) * PT)} "
                   f"{_f(bw * PT)} {_f(bar_h * PT)} re")
    ops.append("f")
    # Courier advance is 0.6 em, so centring needs no font metrics.
    size = TEXT_SIZE_MM
    tw = 0.6 * size * len(label)
    tx = x0 + (w - tw) / 2
    ty = ph - (y0 + PAD_MM + TEXT_BAND_MM - 0.7)
    ops.append(f"BT /F1 {_f(size * PT)} Tf {_f(tx * PT)} {_f(ty * PT)} Td ({label}) Tj ET")
    return ops


def outline_ops(x0, y0, s):
    ph = s["page_h"]
    return [f"0.2 w 0.6 G {_f(x0 * PT)} {_f((ph - y0 - s['label_h']) * PT)} "
            f"{_f(s['label_w'] * PT)} {_f(s['label_h'] * PT)} re S 0 G"]


def check_offsets(s, offset_x, offset_y):
    """Every label position, shifted, must still lie wholly on the page.

    The label box holds the quiet zones and bars, so this rejects any offset
    that would clip a symbol instead of printing a label that cannot scan.
    """
    for v in (offset_x, offset_y):
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
            raise LabelError("offsets must be finite numbers")
    left = s["left"] + offset_x
    top = s["top"] + offset_y
    right = left + (s["cols"] - 1) * s["pitch_x"] + s["label_w"]
    bottom = top + (s["rows"] - 1) * s["pitch_y"] + s["label_h"]
    if left < -1e-6 or top < -1e-6 or right > s["page_w"] + 1e-6 or bottom > s["page_h"] + 1e-6:
        lo_x, hi_x = 0.0 - s["left"], s["page_w"] - right + offset_x
        lo_y, hi_y = 0.0 - s["top"], s["page_h"] - bottom + offset_y
        raise LabelError(f"offset {offset_x:g},{offset_y:g} mm moves labels off the page "
                         f"(x allows {lo_x + 0:g}..{hi_x + 0:g}, y allows {lo_y + 0:g}..{hi_y + 0:g})")


def build_pdf(labels, stock, skip=0, offset_x=0.0, offset_y=0.0, outline=False):
    """Return PDF bytes. Deterministic: same input, same bytes."""
    s = check_stock(dict(stock))
    check_offsets(s, offset_x, offset_y)
    per = int(s["cols"]) * int(s["rows"])
    if not labels:
        raise LabelError("no labels")
    if not 0 <= skip < per:
        raise LabelError(f"--skip must be 0..{per - 1}")
    slots = [None] * skip + list(labels)
    pages = []
    for p in range(0, len(slots), per):
        ops = []
        for i, lab in enumerate(slots[p:p + per]):
            if lab is None:
                continue
            r, c = divmod(i, int(s["cols"]))
            x0 = s["left"] + c * s["pitch_x"] + offset_x
            y0 = s["top"] + r * s["pitch_y"] + offset_y
            if outline:
                ops += outline_ops(x0, y0, s)
            ops += label_ops(lab, x0, y0, s)
        pages.append("\n".join(ops).encode("ascii"))

    objs = []  # index i holds object number i + 1

    def add(body):
        objs.append(body)
        return len(objs)

    catalog = add(None)
    pages_obj = add(None)
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold /Encoding /WinAnsiEncoding >>")
    kids = []
    mb = f"[0 0 {_f(s['page_w'] * PT)} {_f(s['page_h'] * PT)}]"
    for content in pages:
        data = zlib.compress(content, 9)
        stream = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(data) + data + b"\nendstream")
        kids.append(add(f"<< /Type /Page /Parent {pages_obj} 0 R /MediaBox {mb} "
                        f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {stream} 0 R >>".encode()))
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_obj} 0 R >>".encode()
    objs[pages_obj - 1] = (f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] "
                           f"/Count {len(kids)} >>").encode()

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, catalog, xref)
    return bytes(out)


# --- CLI -------------------------------------------------------------------

def spec():
    """The tables the web page must agree with (see tests/golden.json)."""
    return {"code39": CODE39, "media": MEDIA, "stocks": STOCKS, "narrow_mm": NARROW_MM,
            "ratio": RATIO, "quiet_min_mm": QUIET_MIN_MM, "bar_min_mm": BAR_MIN_MM,
            "text_band_mm": TEXT_BAND_MM, "text_size_mm": TEXT_SIZE_MM, "pad_mm": PAD_MM}


CHECK_NOTE = ("last char equals the Code 39 mod-43 check of the first 7; "
              "make sure readers are not set to strip check characters")


def _labels(a):
    if a.first:
        if a.media is None:
            raise LabelError("--first needs --media")
        return [v + a.media for v in volser_range(a.first, a.count)]
    if a.volser:
        return [v if len(v) == 8 or a.media is None else v + a.media for v in a.volser]
    raise LabelError("give --first/--count or VOLSER arguments")


def main(argv=None):
    p = argparse.ArgumentParser(prog="tapelabel", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="check labels; exit 1 if any is invalid")
    v.add_argument("label", nargs="+", help="8-char label, e.g. TAP001L9")
    v.add_argument("--strict", action="store_true", help="treat warnings as errors")

    g = sub.add_parser("pdf", help="write a sheet of labels")
    g.add_argument("volser", nargs="*", help="8-char labels, or 6-char volsers with --media")
    g.add_argument("--first", help="first volser of a range, e.g. TAP000")
    g.add_argument("--count", type=int, default=1)
    g.add_argument("--media", choices=sorted(MEDIA))
    g.add_argument("--stock", choices=sorted(STOCKS), default="letter-2x15")
    g.add_argument("--stock-json", help="JSON file with custom stock geometry (mm)")
    g.add_argument("--skip", type=int, default=0, help="leave the first N positions empty (partly used sheet)")
    g.add_argument("--offset-x", type=float, default=0.0, help="shift everything right by this many mm")
    g.add_argument("--offset-y", type=float, default=0.0, help="shift everything down by this many mm")
    g.add_argument("--outline", action="store_true", help="draw label outlines (for test prints on plain paper)")
    g.add_argument("--strict", action="store_true")
    g.add_argument("-o", "--output", required=True, help="PDF path, or - for stdout")

    sub.add_parser("stocks", help="list built-in stock layouts")
    sub.add_parser("spec-json", help="print the symbology and layout tables as JSON")

    a = p.parse_args(argv)
    try:
        if a.cmd == "stocks":
            for name, s in STOCKS.items():
                print(f"{name:12} {s['cols']}x{s['rows']} on {s['page_w']}x{s['page_h']} mm, "
                      f"label {s['label_w']}x{s['label_h']} mm")
            return 0
        if a.cmd == "spec-json":
            print(json.dumps(spec(), indent=1, sort_keys=True))
            return 0
        if a.cmd == "validate":
            bad = 0
            for lab in a.label:
                try:
                    for w in check(lab, a.strict):
                        print(f"warning: {w}", file=sys.stderr)
                    print(f"ok {lab} ({MEDIA[lab[6:]]})")
                    if looks_checked(lab):
                        print(f"note: {lab}: {CHECK_NOTE}", file=sys.stderr)
                except LabelError as e:
                    print(f"error: {e}", file=sys.stderr)
                    bad += 1
            return 1 if bad else 0
        labels = _labels(a)
        if len(set(labels)) != len(labels):
            raise LabelError("duplicate labels in batch")
        for lab in labels:
            for w in check(lab, a.strict):
                print(f"warning: {w}", file=sys.stderr)
        lookalikes = [lab for lab in labels if looks_checked(lab)]
        if lookalikes:
            print(f"note: {len(lookalikes)} label(s), e.g. {lookalikes[0]}: {CHECK_NOTE}", file=sys.stderr)
        if a.stock_json:
            with open(a.stock_json) as fh:
                stock = json.load(fh)
            if not isinstance(stock, dict):
                raise LabelError("stock JSON must be an object")
        else:
            stock = STOCKS[a.stock]
        pdf = build_pdf(labels, stock, a.skip, a.offset_x, a.offset_y, a.outline)
        if a.output == "-":
            sys.stdout.buffer.write(pdf)
        else:
            with open(a.output, "wb") as fh:
                fh.write(pdf)
        return 0
    except (LabelError, OSError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
