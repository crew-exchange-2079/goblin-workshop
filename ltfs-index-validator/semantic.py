"""Semantic checks for LTFS 2.4.0 full-index XML.

Runs only on a tree that already passed the schema, so every required element
exists and every integer/boolean/partition/datetime has its lexical form. Each
diagnostic carries a fixed code, a severity, and the element's line and path;
no input-derived text (names, values, numbers) ever leaves this module.

Rules and where they come from are listed in RULES.md. Sources: the SNIA LTFS
Format Specification v2.4.0 and the read-only reference implementation pinned in
PROVENANCE.json (xml_reader_libltfs.c, ltfs_internal.c, pathname.c, ltfs.h).
"""
import base64
import binascii
import re
import unicodedata

ERROR, WARNING = "error", "warning"

MAX_NAME_CODEPOINTS = 255       # ltfs.h LTFS_FILENAME_MAX
MAX_COMMENT_BYTES = 65536       # ltfs.h INDEX_MAX_COMMENT_LEN
MAX_XATTR_VALUE_BYTES = 4096    # ltfs.h LTFS_MAX_XATTR_SIZE (implementation limit: warning)
FIRST_INDEX_BLOCK = 5           # VOL1(0) FM(1) label(2) FM(3) FM(4) index(5)
FIRST_DATA_BLOCK = 4            # blocks 0-3 are always the label construct
MIN_INDEX_GAP = 3               # index(b) FM(b+1) FM(b+2) index(b+3)
MAX_STORED = 1000               # diagnostics kept; all are counted

TIME_TAGS = ("updatetime", "creationtime", "changetime", "modifytime",
             "accesstime", "backuptime")
_TRUE = ("true", "1")
_HEX = frozenset("0123456789abcdefABCDEF")
_FORBIDDEN = frozenset("\x00￾￿")
_XML_WS = re.compile(r"[ \t\r\n]")
_DT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\.\d{9}Z$")


class Collector:
    """Counts every diagnostic, keeps the first MAX_STORED."""

    def __init__(self, root):
        self.tree = root.getroottree()
        self.items, self.errors, self.warnings = [], 0, 0

    def add(self, code, severity, el):
        if severity == ERROR:
            self.errors += 1
        else:
            self.warnings += 1
        if len(self.items) < MAX_STORED:
            self.items.append({"code": code, "severity": severity,
                               "line": el.sourceline or 0, "path": self.tree.getpath(el)})


def _child(el, tag):
    return el.find(tag)


def _text(el, tag):
    c = el.find(tag)
    return None if c is None else (c.text or "")


def _int(el, tag):
    return int(el.find(tag).text.strip())


def _bool(el, tag):
    c = el.find(tag)
    return c is not None and (c.text or "").strip() in _TRUE


def _pos(el):
    """(partition, startblock) of a tapeposition element, or None if absent."""
    if el is None:
        return None
    return (el.find("partition").text.strip(), _int(el, "startblock"))


def decode_name(el):
    """Return (name, None) or (None, problem code) for a nametype element."""
    raw = el.text or ""
    if (el.get("percentencoded") or "false").strip() not in _TRUE:
        return raw, None
    out, i = bytearray(), 0
    while i < len(raw):
        if raw[i] == "%":
            h = raw[i + 1:i + 3]
            if len(h) != 2 or not set(h) <= _HEX:
                return None, "NAME_PERCENT_ENCODING_MALFORMED"
            out.append(int(h, 16))
            i += 3
        else:
            out += raw[i].encode("utf-8")
            i += 1
    try:
        return out.decode("utf-8"), None  # strict: rejects overlongs and surrogates
    except UnicodeDecodeError:
        return None, "NAME_NOT_UTF8"


def check_name(el, kind, out):
    """Validate a nametype element; return its NFC form, or None if unusable.

    kind: "entry" (file/dir), "root" (volume name, may be empty), "key"
    (xattr key), "target" (symlink target, may contain '/'), "pattern"
    (placement-policy name pattern).
    """
    name, problem = decode_name(el)
    if problem:
        out.add(problem, ERROR, el)
        return None
    if not name:
        if kind != "root":
            out.add("NAME_EMPTY", ERROR, el)
        return name
    if any(c in _FORBIDDEN for c in name) or (kind != "target" and "/" in name):
        out.add("NAME_FORBIDDEN_CHAR", ERROR, el)
        return None
    nfc = unicodedata.normalize("NFC", name)
    if nfc != name:
        out.add("NAME_NOT_NFC", WARNING, el)
    if kind != "target" and len(nfc) > MAX_NAME_CODEPOINTS:
        out.add("NAME_TOO_LONG", ERROR, el)
    if kind == "entry" and nfc in (".", ".."):
        out.add("NAME_DOT_ENTRY", WARNING, el)
    return nfc


def _valid_datetime(text):
    m = _DT_RE.match(text or "")
    if not m:
        return False
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    if not 1 <= mo <= 12 or h > 23 or mi > 59 or s > 60:  # 60: leap second
        return False
    leap = y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)
    days = (31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)[mo - 1]
    return 1 <= d <= days


def _check_xattrs(entry, out):
    xa = _child(entry, "extendedattributes")
    if xa is None:
        return
    seen = set()
    for x in xa.iterfind("xattr"):
        key_el = x.find("key")
        key = check_name(key_el, "key", out)
        if key is not None:
            if key in seen:
                out.add("XATTR_KEY_DUPLICATE", ERROR, key_el)
            seen.add(key)
        v = x.find("value")
        text = v.text or ""
        if (v.get("type") or "text").strip() == "base64":
            try:
                size = len(base64.b64decode(_XML_WS.sub("", text), validate=True))
            except (binascii.Error, ValueError):
                out.add("XATTR_BASE64_INVALID", ERROR, v)
                continue
        else:
            size = len(text.encode("utf-8"))
        if size > MAX_XATTR_VALUE_BYTES:
            out.add("XATTR_VALUE_LARGE", WARNING, v)


def _check_extents(f, loc, back, out, partitions, facts, blocksize, tape):
    info = _child(f, "extentinfo")
    if info is None:
        return
    length = _int(f, "length")
    rows = []
    for x in info.iterfind("extent"):
        part = x.find("partition").text.strip()
        sb, bo = _int(x, "startblock"), _int(x, "byteoffset")
        bc, fo = _int(x, "bytecount"), _int(x, "fileoffset")
        partitions.add(part)
        if fo + bc > length:
            out.add("EXTENT_BEYOND_LENGTH", ERROR, x)
        if sb < FIRST_DATA_BLOCK:
            out.add("EXTENT_IN_LABEL_AREA", ERROR, x)
        if part == loc[0] and sb >= loc[1]:
            out.add("EXTENT_AFTER_INDEX", ERROR, x)
        if facts and part not in facts:
            out.add("EXTENT_PARTITION_UNKNOWN", ERROR, x)
        last = sb
        if blocksize:
            if bo >= blocksize:
                out.add("EXTENT_BYTEOFFSET_EXCEEDS_BLOCK", ERROR, x)
            last = sb + (bo + bc - 1) // blocksize
            start = sb * blocksize + bo
            tape.append((part, start, start + bc, x))
        if back and back[0] == part and sb <= back[1] <= last:
            out.add("EXTENT_COVERS_INDEX", ERROR, x)
        rows.append((fo, bc, x))
    if any(rows[i][0] > rows[i + 1][0] for i in range(len(rows) - 1)):
        out.add("EXTENT_UNSORTED", WARNING, info)
    end = None
    for fo, bc, x in sorted(rows, key=lambda r: (r[0], r[1])):
        if end is not None and fo < end:
            out.add("EXTENT_OVERLAP", ERROR, x)
        end = fo + bc if end is None else max(end, fo + bc)


def _check_tape_sharing(tape, out):
    """With a known block size, data shared on tape by two extents is a warning."""
    end = {}
    for part, start, stop, x in sorted(tape, key=lambda t: (t[0], t[1], t[2])):
        if start < end.get(part, -1):
            out.add("EXTENT_TAPE_SHARED", WARNING, x)
        end[part] = max(end.get(part, -1), stop)


def _check_pointers(root, loc, back, out, partitions, facts):
    loc_el = root.find("location")
    back_el = root.find("previousgenerationlocation")
    partitions.add(loc[0])
    if loc[1] < FIRST_INDEX_BLOCK:
        out.add("LOCATION_IN_LABEL_AREA", ERROR, loc_el)
    if facts and loc[0] not in facts:
        out.add("LOCATION_PARTITION_UNKNOWN", ERROR, loc_el)
    if back is None:
        if loc[1] > FIRST_INDEX_BLOCK:
            out.add("BACKPTR_MISSING", WARNING, loc_el)
        return
    partitions.add(back[0])
    if back[1] < FIRST_INDEX_BLOCK:
        out.add("BACKPTR_IN_LABEL_AREA", ERROR, back_el)
    if facts and back[0] != facts[1]:
        out.add("BACKPTR_NOT_DATA_PARTITION", ERROR, back_el)
    if back[0] == loc[0]:
        if back[1] == loc[1]:
            out.add("BACKPTR_IS_SELF", WARNING, back_el)
        elif back[1] > loc[1] - MIN_INDEX_GAP:
            out.add("BACKPTR_NOT_BEFORE_SELF", ERROR, back_el)


def summarize(root):
    """Facts used for cross-index (generation chain) checks."""
    return {
        "volumeuuid": _text(root, "volumeuuid").strip().lower(),
        "generation": _int(root, "generationnumber"),
        "updatetime": _text(root, "updatetime").strip(),
        "location": _pos(root.find("location")),
        "backptr": _pos(root.find("previousgenerationlocation")),
        "highestfileuid": _int(root, "highestfileuid"),
    }


def check_index(root, partitions_facts=None, blocksize=None):
    """Run every single-index semantic check; return a Collector.

    partitions_facts: optional (index_partition, data_partition) from the label.
    blocksize: optional label block size in bytes, enables tape-position checks.
    """
    out = Collector(root)
    facts = tuple(partitions_facts) if partitions_facts else None
    loc = _pos(root.find("location"))
    back = _pos(root.find("previousgenerationlocation"))
    partitions = set()
    _check_pointers(root, loc, back, out, partitions, facts)

    comment = root.find("comment")
    if comment is not None and len((comment.text or "").encode("utf-8")) > MAX_COMMENT_BYTES:
        out.add("COMMENT_TOO_LONG", ERROR, comment)
    for el in root.iter(*TIME_TAGS):
        if not _valid_datetime((el.text or "").strip()):
            out.add("DATETIME_INVALID", ERROR, el)
    policy = root.find("dataplacementpolicy")
    if policy is not None:
        for n in policy.iter("name"):
            check_name(n, "pattern", out)

    uids, max_uid, tape = set(), 0, []
    rootdir = root.find("directory")
    stack = [(rootdir, True)]
    while stack:
        entry, is_root = stack.pop()
        uid_el = entry.find("fileuid")
        uid = _int(entry, "fileuid")
        max_uid = max(max_uid, uid)
        if is_root and uid != 1:
            out.add("ROOT_UID_NOT_ONE", ERROR, uid_el)
        elif uid == 0:
            out.add("UID_ZERO", ERROR, uid_el)
        if uid in uids:
            out.add("UID_DUPLICATE", ERROR, uid_el)
        uids.add(uid)
        if is_root:
            check_name(entry.find("name"), "root", out)
        _check_xattrs(entry, out)
        if entry.tag == "file":
            _check_extents(entry, loc, back, out, partitions, facts, blocksize, tape)
            target = entry.find("symlink")
            if target is not None:
                check_name(target, "target", out)
            continue
        names, folded = set(), set()
        children = [c for c in entry.find("contents") if c.tag in ("file", "directory")]
        for c in children:
            name_el = c.find("name")
            name = check_name(name_el, "entry", out)
            if name:
                if name in names:
                    out.add("NAME_DUPLICATE", ERROR, name_el)
                elif name.casefold() in folded:
                    out.add("NAME_CASE_COLLISION", WARNING, name_el)
                names.add(name)
                folded.add(name.casefold())
        stack.extend((c, False) for c in reversed(children))

    if _int(root, "highestfileuid") < max_uid:
        out.add("HIGHESTFILEUID_TOO_LOW", ERROR, root.find("highestfileuid"))
    if len(partitions) > 2:
        out.add("PARTITION_COUNT_EXCEEDED", ERROR, root.find("location"))
    if tape:
        _check_tape_sharing(tape, out)
    out.items.sort(key=lambda d: d["line"])
    return out


def check_chain(root, current, previous, out):
    """Compare this index with the index its previousgenerationlocation names."""
    loc_el = root.find("location")
    if current["volumeuuid"] != previous["volumeuuid"]:
        out.add("PREVIOUS_VOLUME_MISMATCH", ERROR, root.find("volumeuuid"))
    back_el = root.find("previousgenerationlocation")
    if current["backptr"] != previous["location"]:
        out.add("PREVIOUS_NOT_REFERENCED", ERROR, back_el if back_el is not None else loc_el)
    gen_el = root.find("generationnumber")
    if current["generation"] < previous["generation"]:
        out.add("GENERATION_REGRESSION", ERROR, gen_el)
    elif (current["generation"] == previous["generation"]
          and current["location"][0] == previous["location"][0]):
        out.add("GENERATION_NOT_INCREASING", ERROR, gen_el)
    if current["highestfileuid"] < previous["highestfileuid"]:
        out.add("HIGHESTFILEUID_REGRESSION", ERROR, root.find("highestfileuid"))
    if current["updatetime"] < previous["updatetime"]:
        out.add("UPDATETIME_REGRESSION", WARNING, root.find("updatetime"))
