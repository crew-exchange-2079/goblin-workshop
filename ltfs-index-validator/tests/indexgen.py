"""In-memory model of an LTFS 2.4.0 index, its XML serialiser, and mutations.

Shared by the hypothesis property tests and make_corpus.py. Everything built
through `Index` serialises to schema-valid XML; a mutation changes the model in
one way that must produce one known diagnostic code.
"""
import copy
import random
import unicodedata
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

T0 = "2026-01-01T00:00:00.000000000Z"


@dataclass
class Extent:
    partition: str
    startblock: int
    byteoffset: int
    bytecount: int
    fileoffset: int


@dataclass
class Xattr:
    key: str
    value: str
    vtype: str = "text"  # "text" | "base64"; base64 values are stored already encoded


@dataclass
class Entry:
    name: str
    uid: int
    is_dir: bool = False
    children: list = field(default_factory=list)
    length: int = 0
    extents: list = None      # list[Extent] for a regular file with extentinfo
    symlink: str = None
    xattrs: list = field(default_factory=list)
    times: dict = field(default_factory=dict)
    raw_name: bool = False    # True: write `name` verbatim with percentencoded="true"


@dataclass
class Index:
    root: Entry
    generation: int = 1
    location: tuple = ("b", 20)
    backptr: tuple = ("b", 5)
    highestfileuid: int = None
    volumeuuid: str = "00000000-0000-4000-8000-000000000000"
    updatetime: str = T0
    creator: str = "synthetic fixture"
    comment: str = None
    policy: list = None       # list of name patterns
    lockstate: str = None

    def entries(self):
        stack = [self.root]
        while stack:
            e = stack.pop()
            yield e
            stack.extend(reversed(e.children))

    def files_with_extents(self):
        return [e for e in self.entries() if not e.is_dir and e.extents]

    def max_uid(self):
        return max(e.uid for e in self.entries())

    def max_extent_block(self, partition):
        return max((x.startblock for e in self.entries() if e.extents for x in e.extents
                    if x.partition == partition), default=0)


def needs_encoding(ch):
    """Characters that cannot appear literally (and survive parsing) in XML text."""
    o = ord(ch)
    return (ch in "%\r" or (o < 0x20 and ch not in "\t\n") or 0xD800 <= o <= 0xDFFF
            or o in (0xFFFE, 0xFFFF) or 0x7F <= o <= 0x9F)


def percent_encode(name):
    return "".join("".join("%%%02X" % b for b in c.encode("utf-8")) if needs_encoding(c) else c
                   for c in name)


def _name_xml(tag, name, raw=False):
    if raw:
        return '<%s percentencoded="true">%s</%s>' % (tag, escape(name), tag)
    if any(needs_encoding(c) for c in name):
        return '<%s percentencoded="true">%s</%s>' % (tag, escape(percent_encode(name)), tag)
    return "<%s>%s</%s>" % (tag, escape(name), tag)


def _all(rng, parts):
    """Children of an xs:all group, optionally permuted."""
    if rng is not None:
        parts = list(parts)
        rng.shuffle(parts)
    return "".join(parts)


def _pos(tag, pos, rng):
    return "<%s>%s</%s>" % (tag, _all(rng, ["<partition>%s</partition>" % pos[0],
                                           "<startblock>%d</startblock>" % pos[1]]), tag)


def _entry_xml(e, rng):
    parts = [_name_xml("name", e.name, e.raw_name), "<fileuid>%d</fileuid>" % e.uid,
             "<readonly>false</readonly>"]
    for t in ("creationtime", "changetime", "modifytime", "accesstime", "backuptime"):
        parts.append("<%s>%s</%s>" % (t, e.times.get(t, T0), t))
    if e.xattrs:
        parts.append("<extendedattributes>%s</extendedattributes>" % "".join(
            "<xattr>%s</xattr>" % _all(rng, [
                _name_xml("key", x.key),
                ('<value type="base64">%s</value>' % escape(x.value)) if x.vtype == "base64"
                else "<value>%s</value>" % escape(x.value)]) for x in e.xattrs))
    if e.is_dir:
        parts.append("<contents>%s</contents>" % "".join(_entry_xml(c, rng) for c in e.children))
        return "<directory>%s</directory>" % _all(rng, parts)
    parts.append("<length>%d</length>" % e.length)
    if e.symlink is not None:
        parts.append(_name_xml("symlink", e.symlink))
    elif e.extents is not None:
        parts.append("<extentinfo>%s</extentinfo>" % "".join(
            "<extent>%s</extent>" % _all(rng, [
                "<partition>%s</partition>" % x.partition, "<startblock>%d</startblock>" % x.startblock,
                "<byteoffset>%d</byteoffset>" % x.byteoffset, "<bytecount>%d</bytecount>" % x.bytecount,
                "<fileoffset>%d</fileoffset>" % x.fileoffset]) for x in e.extents))
    return "<file>%s</file>" % _all(rng, parts)


def to_xml(idx, order_seed=None):
    rng = random.Random(order_seed) if order_seed is not None else None
    highest = idx.highestfileuid if idx.highestfileuid is not None else idx.max_uid()
    parts = ["<creator>%s</creator>" % escape(idx.creator),
             "<volumeuuid>%s</volumeuuid>" % idx.volumeuuid,
             "<generationnumber>%d</generationnumber>" % idx.generation,
             "<updatetime>%s</updatetime>" % idx.updatetime,
             _pos("location", idx.location, rng),
             "<allowpolicyupdate>true</allowpolicyupdate>",
             "<highestfileuid>%d</highestfileuid>" % highest,
             _entry_xml(idx.root, rng)]
    if idx.backptr is not None:
        parts.append(_pos("previousgenerationlocation", idx.backptr, rng))
    if idx.comment is not None:
        parts.append("<comment>%s</comment>" % escape(idx.comment))
    if idx.policy is not None:
        parts.append("<dataplacementpolicy><indexpartitioncriteria>%s<size>1048576</size>"
                     "</indexpartitioncriteria></dataplacementpolicy>"
                     % "".join(_name_xml("name", n) for n in idx.policy))
    if idx.lockstate is not None:
        parts.append("<volumelockstate>%s</volumelockstate>" % idx.lockstate)
    body = _all(rng, parts)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<ltfsindex version="2.4.0">%s</ltfsindex>\n'
            % body).encode("utf-8")


# --- a small deterministic builder used by the corpus and unit tests ----------

def sample_index():
    """Root with a subdirectory, a two-extent file, a sparse file, a symlink and xattrs."""
    a = Entry("a.bin", 3, length=612, extents=[Extent("b", 10, 0, 512, 0), Extent("b", 11, 0, 100, 512)],
              xattrs=[Xattr("user.note", "hello"), Xattr("user.blob", "AAEC/w==", "base64")])
    sparse = Entry("sparse.img", 5, length=1 << 20,
                   extents=[Extent("b", 12, 0, 4096, 0), Extent("b", 13, 0, 4096, 1 << 19)])
    sub = Entry("sub", 2, is_dir=True, children=[a, sparse])
    link = Entry("link", 4, symlink="sub/a.bin")
    small = Entry("café 日本", 6, length=10, extents=[Extent("a", 6, 0, 10, 0)])
    ctrl = Entry("tab\there\x01", 7, length=0, extents=[])
    root = Entry("vol", 1, is_dir=True, children=[sub, link, small, ctrl],
                 xattrs=[Xattr("ltfs.volumeName", "vol")])
    return Index(root, generation=3, location=("b", 20), backptr=("b", 15), highestfileuid=9,
                 comment="synthetic", policy=["*.txt"], lockstate="unlocked")


# --- mutations: each returns a mutated deep copy, or None when not applicable --

def _pick(rng, items):
    return rng.choice(items) if items else None


def _non_root(idx):
    return [e for e in idx.entries() if e is not idx.root]


def _dirs_with(idx, n):
    return [e for e in idx.entries() if e.is_dir and len(e.children) >= n]


def m_uid_duplicate(idx, rng):
    es = _non_root(idx)
    if len(es) < 1:
        return None
    a = _pick(rng, es)
    b = _pick(rng, [e for e in idx.entries() if e is not a])
    a.uid = b.uid
    return idx


def m_uid_zero(idx, rng):
    e = _pick(rng, _non_root(idx))
    if e is None:
        return None
    e.uid = 0
    return idx


def m_root_uid(idx, rng):
    idx.root.uid = idx.max_uid() + 1
    idx.highestfileuid = max(idx.highestfileuid or 0, idx.root.uid)
    return idx


def m_highest_low(idx, rng):
    idx.highestfileuid = idx.max_uid() - 1
    return idx


def m_extent_overlap(idx, rng):
    f = _pick(rng, idx.files_with_extents())
    if f is None:
        return None
    x = _pick(rng, f.extents)
    f.extents.insert(f.extents.index(x) + 1, copy.copy(x))
    return idx


def m_extent_beyond(idx, rng):
    f = _pick(rng, idx.files_with_extents())
    if f is None:
        return None
    f.length = max(x.fileoffset + x.bytecount for x in f.extents) - 1
    return idx


def m_extent_after_index(idx, rng):
    f = _pick(rng, idx.files_with_extents())
    if f is None:
        return None
    x = _pick(rng, f.extents)
    x.partition, x.startblock = idx.location[0], idx.location[1] + rng.randint(0, 3)
    return idx


def m_extent_label_area(idx, rng):
    f = _pick(rng, idx.files_with_extents())
    if f is None:
        return None
    _pick(rng, f.extents).startblock = rng.randint(0, 3)
    return idx


def m_location_label_area(idx, rng):
    idx.location = (idx.location[0], rng.randint(0, 4))
    return idx


def m_backptr_label_area(idx, rng):
    idx.backptr = (idx.location[0], rng.randint(0, 4))
    return idx


def m_backptr_not_before(idx, rng):
    p, b = idx.location
    idx.backptr = (p, rng.choice([b - 2, b - 1, b + 1, b + rng.randint(2, 50)]))
    return idx


def m_partition_count(idx, rng):
    f = _pick(rng, idx.files_with_extents())
    if f is None:
        return None
    idx.backptr = ("y", 5)
    _pick(rng, f.extents).partition = "z"
    return idx


def _rename(idx, rng, name, raw=False):
    e = _pick(rng, _non_root(idx))
    if e is None:
        return None
    e.name, e.raw_name = name, raw
    return idx


def m_name_slash(idx, rng):
    name, raw = rng.choice([("a/b", False), ("/", False), ("x%2Fy", True), ("nul%00", True),
                            ("%EF%BF%BE", True)])
    return _rename(idx, rng, name, raw)


def m_name_empty(idx, rng):
    return _rename(idx, rng, "")


def m_name_long(idx, rng):
    return _rename(idx, rng, rng.choice(["x", "é", "\U0001F600"]) * rng.randint(256, 300))


def m_name_bad_percent(idx, rng):
    return _rename(idx, rng, rng.choice(["%G1", "abc%", "%4", "%%41"]), raw=True)


def m_name_not_utf8(idx, rng):
    return _rename(idx, rng, rng.choice(["%C3%28", "%FF", "%ED%A0%80", "%C0%AF"]), raw=True)


def m_name_duplicate(idx, rng):
    d = _pick(rng, _dirs_with(idx, 2))
    if d is None:
        return None
    a, b = rng.sample(d.children, 2)
    a.name, a.raw_name = b.name, b.raw_name
    return idx


def m_name_not_nfc(idx, rng):
    return _rename(idx, rng, "é-%d" % rng.randint(0, 9))


def m_xattr_base64(idx, rng):
    e = _pick(rng, list(idx.entries()))
    e.xattrs = [x for x in e.xattrs if x.key != "zz.bad"] + [
        Xattr("zz.bad", rng.choice(["@@@@", "AAE", "A===", "AAEC/w=x"]), "base64")]
    return idx


def m_xattr_dup_key(idx, rng):
    e = _pick(rng, list(idx.entries()))
    e.xattrs = list(e.xattrs) + [Xattr("zz.dup", "1"), Xattr("zz.dup", "2")]
    return idx


def m_xattr_large(idx, rng):
    e = _pick(rng, list(idx.entries()))
    e.xattrs = list(e.xattrs) + [Xattr("zz.large", "a" * rng.randint(4097, 6000))]
    return idx


def m_datetime(idx, rng):
    bad = rng.choice(["2026-13-01T00:00:00.000000000Z", "2026-02-30T00:00:00.000000000Z",
                      "2025-02-29T00:00:00.000000000Z", "2026-01-01T24:00:00.000000000Z",
                      "2026-01-01T00:60:00.000000000Z", "2026-00-10T00:00:00.000000000Z"])
    if rng.random() < 0.5:
        idx.updatetime = bad
    else:
        _pick(rng, list(idx.entries())).times["modifytime"] = bad
    return idx


def m_comment_long(idx, rng):
    idx.comment = "c" * (65536 + rng.randint(1, 100))
    return idx


def m_backptr_missing(idx, rng):
    idx.backptr = None
    idx.location = (idx.location[0], max(idx.location[1], 6))
    return idx


# code -> (mutation, severity)
MUTATIONS = {
    "UID_DUPLICATE": (m_uid_duplicate, "error"),
    "UID_ZERO": (m_uid_zero, "error"),
    "ROOT_UID_NOT_ONE": (m_root_uid, "error"),
    "HIGHESTFILEUID_TOO_LOW": (m_highest_low, "error"),
    "EXTENT_OVERLAP": (m_extent_overlap, "error"),
    "EXTENT_BEYOND_LENGTH": (m_extent_beyond, "error"),
    "EXTENT_AFTER_INDEX": (m_extent_after_index, "error"),
    "EXTENT_IN_LABEL_AREA": (m_extent_label_area, "error"),
    "LOCATION_IN_LABEL_AREA": (m_location_label_area, "error"),
    "BACKPTR_IN_LABEL_AREA": (m_backptr_label_area, "error"),
    "BACKPTR_NOT_BEFORE_SELF": (m_backptr_not_before, "error"),
    "PARTITION_COUNT_EXCEEDED": (m_partition_count, "error"),
    "NAME_FORBIDDEN_CHAR": (m_name_slash, "error"),
    "NAME_EMPTY": (m_name_empty, "error"),
    "NAME_TOO_LONG": (m_name_long, "error"),
    "NAME_PERCENT_ENCODING_MALFORMED": (m_name_bad_percent, "error"),
    "NAME_NOT_UTF8": (m_name_not_utf8, "error"),
    "NAME_DUPLICATE": (m_name_duplicate, "error"),
    "NAME_NOT_NFC": (m_name_not_nfc, "warning"),
    "XATTR_BASE64_INVALID": (m_xattr_base64, "error"),
    "XATTR_KEY_DUPLICATE": (m_xattr_dup_key, "error"),
    "XATTR_VALUE_LARGE": (m_xattr_large, "warning"),
    "DATETIME_INVALID": (m_datetime, "error"),
    "COMMENT_TOO_LONG": (m_comment_long, "error"),
    "BACKPTR_MISSING": (m_backptr_missing, "warning"),
}


def mutate(idx, code, rng):
    """Deep-copy idx and apply the mutation for `code`; None if not applicable."""
    fn, _ = MUTATIONS[code]
    return fn(copy.deepcopy(idx), rng)


def nfc(s):
    return unicodedata.normalize("NFC", s)
