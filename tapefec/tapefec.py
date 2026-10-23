#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""tapefec -- streaming forward-error-correction framer.

    tapefec encode [-k K] [-m M] [--block-size B] < in > out
    tapefec decode < in > out
    tapefec inspect < in
    tapefec damage [--per-group N | --rate P] < in > out

Python 3 standard library only. numpy, when importable, is used as an optional
fast path for the Reed-Solomon arithmetic; its output is byte-identical to the
pure path (see `--engine`). See DESIGN.md for the wire format.
"""

import argparse
import hashlib
import json
import os
import random
import struct
import sys
import zlib

__version__ = "1.1.0"
FORMAT_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)

# ---------------------------------------------------------------- framing ---

MAGIC = b"TAPEFEC\x1a"
HEADER_SIZE = 40
HEADER_BASE = struct.Struct("<8sBBHIIIIII")   # bytes 0..35
assert HEADER_BASE.size == 36

FT_HEADER, FT_DATA, FT_PARITY, FT_TRAILER = 0, 1, 2, 3
FT_NAMES = {FT_HEADER: "header", FT_DATA: "data", FT_PARITY: "parity",
            FT_TRAILER: "trailer"}

STREAM_HEADER_FMT = struct.Struct("<HHIII")
STREAM_HEADER_SIZE = STREAM_HEADER_FMT.size
assert STREAM_HEADER_SIZE == 16

TRAILER_FMT = struct.Struct("<Q32s")
TRAILER_SIZE = TRAILER_FMT.size
assert TRAILER_SIZE == 40

DEFAULT_K = 20
DEFAULT_M = 4
DEFAULT_BLOCK_SIZE = 65536

# Upper bound on an accepted payload_len before the stream header frame has
# told us the real block size. Stops a corrupt length from allocating 4 GiB.
ABSOLUTE_MAX_PAYLOAD = 64 << 20


class TapefecError(Exception):
    """Fatal problem with the stream or the arguments."""


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def pack_header(ftype, stream_id, frame_index, block_id, payload_len,
                payload_crc, group_data_len, reserved=0, version=FORMAT_VERSION):
    head = HEADER_BASE.pack(MAGIC, version, ftype, stream_id,
                            frame_index, block_id, payload_len, payload_crc,
                            group_data_len, reserved)
    return head + struct.pack("<I", crc32(head))


def unpack_header(raw):
    """Return a header dict if `raw` is a plausible frame header, else None."""
    if len(raw) < HEADER_SIZE or raw[:8] != MAGIC:
        return None
    (_m, version, ftype, stream_id, frame_index, block_id, payload_len,
     payload_crc, group_data_len, reserved) = HEADER_BASE.unpack(raw[:36])
    (hcrc,) = struct.unpack_from("<I", raw, 36)
    if version not in SUPPORTED_VERSIONS or ftype not in FT_NAMES:
        return None
    if crc32(bytes(raw[:36])) != hcrc:
        return None
    return dict(version=version, ftype=ftype, stream_id=stream_id,
                frame_index=frame_index, block_id=block_id,
                payload_len=payload_len, payload_crc=payload_crc,
                group_data_len=group_data_len, reserved=reserved)


# ------------------------------------------------------------ GF(2^8) / RS ---

GF_POLY = 0x11D
GF_PRIMITIVE = 0x02


def _build_gf_tables():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        x <<= 1
        if x & 0x100:
            x ^= GF_POLY
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


GF_EXP, GF_LOG = _build_gf_tables()

# _MULTAB[c] is a 256-byte translation table implementing x -> c*x in GF(2^8).
# Multiplying a whole block by a constant is therefore bytes.translate(): one C
# loop, no Python-level per-byte work.
_MULTAB = [None] + [bytes(
    0 if v == 0 else GF_EXP[GF_LOG[c] + GF_LOG[v]] for v in range(256)
) for c in range(1, 256)]


def gf_mul(a, b):
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def gf_inv(a):
    if a == 0:
        raise ZeroDivisionError("0 has no inverse in GF(2^8)")
    return GF_EXP[255 - GF_LOG[a]]


def gf_pow(a, n):
    if a == 0:
        return 1 if n % 255 == 0 else 0
    return GF_EXP[(GF_LOG[a] * n) % 255]


def gf_matrix_invert(mat):
    """Gauss-Jordan inverse of a square matrix over GF(2^8)."""
    n = len(mat)
    work = [list(row) + [1 if i == j else 0 for j in range(n)]
            for i, row in enumerate(mat)]
    for col in range(n):
        piv = next((r for r in range(col, n) if work[r][col]), None)
        if piv is None:
            raise TapefecError("singular GF(2^8) submatrix: code is not MDS")
        work[col], work[piv] = work[piv], work[col]
        inv = gf_inv(work[col][col])
        work[col] = [gf_mul(v, inv) for v in work[col]]
        for r in range(n):
            if r != col and work[r][col]:
                f = work[r][col]
                work[r] = [v ^ gf_mul(f, w)
                           for v, w in zip(work[r], work[col])]
    return [row[n:] for row in work]


def vandermonde_generator_matrix(k, m):
    """Format-v1 matrix: G[i][j] = alpha^(i*j), a generalised Vandermonde.

    MDS for many (k, m) but NOT for all: for k=10, m=6 some e x e
    submatrices are singular (46 of 8007), which makes a lost set that
    should still be repairable abort the whole decode.  Kept so version-1
    streams stay readable.
    """
    return [[gf_pow(GF_PRIMITIVE, i * j) for j in range(k)] for i in range(m)]


def cauchy_generator_matrix(k, m):
    """Format-v2 matrix: G[i][j] = 1 / (x_i XOR y_j) with x_i = i, y_j = m+j.

    A Cauchy matrix: every square submatrix is invertible whenever all
    x_i and y_j are distinct and no x_i equals any y_j.  Here x_i ranges
    over 0..m-1 and y_j over m..m+k-1, so the sets are disjoint and every
    entry is non-zero; hence the code is MDS for every k+m <= 256, i.e. ANY
    m erasures per group are repairable for EVERY (k, m).
    """
    return [[gf_inv(i ^ (m + j)) for j in range(k)] for i in range(m)]


def generator_matrix(k, m, version=FORMAT_VERSION):
    """The generator for a stream of the given format version."""
    if version >= 2:
        return cauchy_generator_matrix(k, m)
    return vandermonde_generator_matrix(k, m)


def rs_encode_group(blocks, length, g, m):
    """blocks: up to k data blocks, each exactly `length` bytes (the caller
    zero-pads a short final group).  Returns m parity blocks of `length`."""
    n = len(blocks)
    ints = [int.from_bytes(b, "big") for b in blocks]
    parity = []
    for i in range(m):
        row = g[i]
        acc = 0
        for j in range(n):
            c = row[j]
            if c == 1:
                acc ^= ints[j]
            elif c != 0:
                acc ^= int.from_bytes(blocks[j].translate(_MULTAB[c]), "big")
        parity.append(acc.to_bytes(length, "big"))
    return parity


class Unrecoverable(Exception):
    def __init__(self, need, have):
        super().__init__("need %d surviving parity blocks, have %d"
                         % (need, have))
        self.need = need
        self.have = have


def rs_repair(known, erased, parity, length, g, m):
    """known: {pos: block}, erased: [pos], parity: {pos: block}.

    Returns {pos: recovered block of `length` bytes}.  Raises Unrecoverable if
    fewer parity rows survive than there are erased data blocks.
    """
    e = len(erased)
    if e == 0:
        return {}
    rows = sorted(parity)
    if len(rows) < e:
        raise Unrecoverable(e, len(rows))
    rows = rows[:e]

    # A short final block is zero-padded at the END to `length`, exactly as
    # the encoder pads it before computing parity, so the codeword symbol
    # it contributes here is the same one the parity was built from.
    known = {pos: b + bytes(length - len(b)) if len(b) < length else b
             for pos, b in known.items()}

    # b_i = p_i XOR sum over known data of G[i][j]*d_j
    b = []
    for i in rows:
        acc = int.from_bytes(parity[i], "big")
        for j, d in known.items():
            c = g[i][j]
            if c == 1:
                acc ^= int.from_bytes(d, "big")
            elif c != 0:
                acc ^= int.from_bytes(d.translate(_MULTAB[c]), "big")
        b.append(acc.to_bytes(length, "big"))

    # u = A^-1 b with A = G[rows][erased]
    a_inv = gf_matrix_invert([[g[i][j] for j in erased] for i in rows])

    out = {}
    for pos, inv_row in zip(erased, a_inv):
        acc = 0
        for t in range(e):
            c = inv_row[t]
            if c == 1:
                acc ^= int.from_bytes(b[t], "big")
            elif c != 0:
                acc ^= int.from_bytes(b[t].translate(_MULTAB[c]), "big")
        out[pos] = acc.to_bytes(length, "big")
    return out


# --------------------------------------------------------- numpy fast path ---
#
# Optional.  The functions above are the reference implementation and need
# nothing outside the standard library.  When numpy is importable the two
# functions below compute the same GF(2^8) products a different way; the
# golden tests in test_tapefec.py pin both engines to the same stream digests.
#
# The trick: every output row is XOR_j c_ij * d_j.  For input block j, pack
# the products c_0j*x, c_1j*x, ... of up to 8 output rows into one machine
# word per byte value x, giving a 256-entry table.  One np.take over block j
# then yields that block's contribution to all of those rows at once, and a
# lane-wise XOR accumulates it.  That is one gather per input block instead
# of one translate (plus a big-integer round trip) per (row, block) pair.

_NUMPY = None          # (numpy module, 256x256 product table); False if absent
_LANES = ((1, "u1"), (2, "u2"), (4, "u4"), (8, "u8"))


def _numpy_tables():
    global _NUMPY
    if _NUMPY is None:
        try:
            import numpy as np
        except ImportError:
            _NUMPY = False
        else:
            mul = np.frombuffer(bytes(256) + b"".join(_MULTAB[1:]),
                                dtype=np.uint8).reshape(256, 256)
            _NUMPY = (np, mul)
    return _NUMPY


def numpy_available():
    return bool(_numpy_tables())


def _np_combine(coeffs, blocks, length):
    """rows[i] = XOR_j coeffs[i][j] * blocks[j] over GF(2^8).  Every block is
    exactly `length` bytes.  Returns a list of len(coeffs) bytes objects."""
    np, mul = _numpy_tables()
    data = [np.frombuffer(b, dtype=np.uint8) for b in blocks]
    out = []
    for r0 in range(0, len(coeffs), 8):
        chunk = coeffs[r0:r0 + 8]
        width, code = next(lane for lane in _LANES if lane[0] >= len(chunk))
        dtype = np.dtype(code)
        cmat = np.zeros((width, len(blocks)), dtype=np.intp)
        cmat[:len(chunk)] = chunk
        # tab[j, x] packs (c_0j*x, c_1j*x, ...) into one word of `width` bytes
        tab = np.ascontiguousarray(mul[cmat].transpose(1, 2, 0)) \
            .view(dtype)[..., 0]
        live = cmat.any(axis=0)
        acc = np.zeros(length, dtype=dtype)
        tmp = np.empty(length, dtype=dtype)
        for j, d in enumerate(data):
            if live[j]:
                # uint8 indices into a 256-entry table never wrap; "wrap"
                # just skips the bounds-check pass that "raise" makes.
                np.take(tab[j], d, out=tmp, mode="wrap")
                acc ^= tmp
        lanes = acc.view(np.uint8).reshape(length, width)
        out.extend(lanes[:, i].tobytes() for i in range(len(chunk)))
    return out


def np_rs_encode_group(blocks, length, g, m):
    """numpy twin of rs_encode_group(): same arguments, same bytes out."""
    return _np_combine([row[:len(blocks)] for row in g[:m]], blocks, length)


def np_rs_repair(known, erased, parity, length, g, m):
    """numpy twin of rs_repair(): same arguments, results and exceptions."""
    e = len(erased)
    if e == 0:
        return {}
    rows = sorted(parity)
    if len(rows) < e:
        raise Unrecoverable(e, len(rows))
    rows = rows[:e]
    known = {pos: b + bytes(length - len(b)) if len(b) < length else b
             for pos, b in known.items()}
    # b_i = p_i XOR sum over known data of G[i][j]*d_j: the parity block is
    # one more input with coefficient 1 in its own row and 0 elsewhere.
    kpos = list(known)
    coeffs = [[g[i][j] for j in kpos] + [int(r == i) for r in rows]
              for i in rows]
    b = _np_combine(coeffs, [known[j] for j in kpos]
                    + [parity[i] for i in rows], length)
    a_inv = gf_matrix_invert([[g[i][j] for j in erased] for i in rows])
    return dict(zip(erased, _np_combine(a_inv, b, length)))


# ------------------------------------------------------------------ engines ---

ENGINES = ("auto", "pure", "numpy")
_default_engine = "auto"


def resolve_engine(name=None):
    """Return (engine name, encode_group, repair) for `name`, or for the
    default chosen with set_engine() when `name` is None.  "auto" is numpy
    when it can be imported, else pure."""
    name = _default_engine if name is None else name
    if name == "auto":
        name = "numpy" if numpy_available() else "pure"
    if name == "pure":
        return "pure", rs_encode_group, rs_repair
    if name == "numpy":
        if not numpy_available():
            raise TapefecError("engine 'numpy' requested but numpy cannot "
                               "be imported")
        return "numpy", np_rs_encode_group, np_rs_repair
    raise TapefecError("unknown engine %r (choose one of %s)"
                       % (name, ", ".join(ENGINES)))


def set_engine(name):
    """Make `name` the default engine.  Raises TapefecError for an unknown
    name, or for "numpy" when numpy cannot be imported.  "auto" is resolved
    only when arithmetic is first needed, so choosing it imports nothing."""
    global _default_engine
    if name != "auto":
        resolve_engine(name)
    _default_engine = name


def _repair_for(engine):
    """The repair function for `engine`.  For "auto" it defers resolving --
    and so importing numpy -- until a group actually needs repair, which an
    undamaged stream never does."""
    name = _default_engine if engine is None else engine
    if name != "auto":
        return resolve_engine(name)[2]

    def repair(*args):
        return resolve_engine("auto")[2](*args)
    return repair


# ------------------------------------------------------------- frame reader ---


class FrameReader:
    """Resynchronising frame reader: finds the magic, validates the header CRC,
    and yields (header, payload_or_None).  payload is None when the payload
    CRC failed, which is how the decoder learns a block is damaged."""

    TRIM_THRESHOLD = 1 << 22

    def __init__(self, fh, max_payload=ABSOLUTE_MAX_PAYLOAD, chunk=1 << 20):
        self.fh = fh
        # read1() returns whatever one read of the pipe delivers instead of
        # blocking until `chunk` bytes arrive, so frames are decoded as soon
        # as they are complete rather than a megabyte at a time.
        self._read = getattr(fh, "read1", fh.read)
        self.max_payload = max_payload
        self.chunk = chunk
        self.buf = bytearray()
        self.pos = 0
        self.eof = False
        self.stats = dict(frames=0, garbage_bytes=0, resyncs=0,
                          bad_payload=0, truncated=0)

    def _pull(self):
        if self.eof:
            return False
        data = self._read(self.chunk)
        if not data:
            self.eof = True
            return False
        self.buf += data
        return True

    def _ensure(self, n):
        while len(self.buf) - self.pos < n:
            if not self._pull():
                return False
        return True

    def _compact(self):
        if self.pos >= self.TRIM_THRESHOLD:
            del self.buf[:self.pos]
            self.pos = 0

    def _seek_magic(self):
        """Move the cursor onto the next plausible frame start."""
        while True:
            idx = self.buf.find(MAGIC, self.pos)
            if idx >= 0:
                if idx > self.pos:
                    self.stats["garbage_bytes"] += idx - self.pos
                    self.stats["resyncs"] += 1
                    self.pos = idx
                return True
            keep_from = len(self.buf) - (len(MAGIC) - 1)
            if keep_from > self.pos:
                self.stats["garbage_bytes"] += keep_from - self.pos
                self.pos = keep_from
            if not self._pull():
                if self.pos < len(self.buf):
                    self.stats["garbage_bytes"] += len(self.buf) - self.pos
                    self.pos = len(self.buf)
                return False

    def frames(self):
        while self._seek_magic():
            if not self._ensure(HEADER_SIZE):
                self.stats["truncated"] += 1
                break
            head = unpack_header(self.buf[self.pos:self.pos + HEADER_SIZE])
            if head is None or head["payload_len"] > self.max_payload:
                self.pos += 1          # false marker or absurd length: resync
                self._compact()
                continue
            total = HEADER_SIZE + head["payload_len"]
            if not self._ensure(total):
                self.stats["truncated"] += 1
                break
            payload = bytes(self.buf[self.pos + HEADER_SIZE:self.pos + total])
            self.pos += total
            self.stats["frames"] += 1
            if crc32(payload) != head["payload_crc"]:
                self.stats["bad_payload"] += 1
                yield head, None
            else:
                yield head, payload
            self._compact()
        self.buf = bytearray()
        self.pos = 0


# ------------------------------------------------------------------ encoder ---


def encode_stream(fin, fout, k=DEFAULT_K, m=DEFAULT_M,
                  block_size=DEFAULT_BLOCK_SIZE, stream_id=0,
                  version=FORMAT_VERSION, engine=None):
    """Read a stream from `fin`, write a FEC stream to `fout`.  Returns a dict
    of encode parameters.  Memory use is bounded at (k+1) blocks.

    `version` selects the stream format (and with it the Reed-Solomon
    generator matrix); it exists so tests can build version-1 streams with
    the old Vandermonde matrix.  New streams always use FORMAT_VERSION.
    `engine` picks the parity arithmetic (see resolve_engine); every engine
    writes the same bytes.  `fout` is flushed after every group, so a pipe
    reader sees each group as soon as it is encoded.
    """
    if version not in SUPPORTED_VERSIONS:
        raise TapefecError("format version must be one of %s, got %r"
                           % (list(SUPPORTED_VERSIONS), version))
    if not 1 <= k <= 255:
        raise TapefecError("k must be 1..255, got %r" % (k,))
    if not 1 <= m <= 255:
        raise TapefecError("m must be 1..255, got %r" % (m,))
    if k + m > 256:
        raise TapefecError("k+m must be <= 256")
    if not isinstance(block_size, int) or not 1 <= block_size <= 0xFFFFFFFF:
        raise TapefecError("block_size must be an integer in 1..4294967295")
    if not 0 <= stream_id <= 0xFFFF:
        raise TapefecError("stream_id must be 0..65535")

    engine, encode_group, _repair = resolve_engine(engine)
    flush = getattr(fout, "flush", lambda: None)
    g = generator_matrix(k, m, version)
    digest = hashlib.sha256()
    total = 0
    frame_index = 0
    block_id = 0
    parity_id = 0

    stream_hdr = STREAM_HEADER_FMT.pack(k, m, block_size, 0, 0)
    fout.write(pack_header(FT_HEADER, stream_id, 0, 0, len(stream_hdr),
                           crc32(stream_hdr), 0, version=version))
    fout.write(stream_hdr)
    frame_index = 1

    zeros = bytes(block_size)
    buf = bytearray()
    group = []
    while True:
        want = block_size - len(buf)
        chunk = fin.read(want) if want else b""
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
        buf += chunk
        if len(buf) == block_size:
            group.append(bytes(buf))
            del buf[:]
            if len(group) == k:
                _flush_group(fout, g, k, m, block_size, stream_id, group,
                             frame_index, block_id, parity_id, zeros,
                             encode_group)
                flush()
                frame_index += k + m
                block_id += k
                parity_id += m
                group = []
    if buf:
        group.append(bytes(buf))
        del buf[:]
    if group:
        _flush_group(fout, g, k, m, block_size, stream_id, group,
                     frame_index, block_id, parity_id, zeros, encode_group)
        frame_index += len(group) + m
        block_id += len(group)
        parity_id += m

    trailer = TRAILER_FMT.pack(total, digest.digest())
    fout.write(pack_header(FT_TRAILER, stream_id, frame_index, 0,
                           TRAILER_SIZE, crc32(trailer), 0, version=version))
    fout.write(trailer)
    return dict(stream_id=stream_id, k=k, m=m, block_size=block_size,
                total_length=total, data_blocks=block_id,
                parity_blocks=parity_id, frame_index=frame_index + 1,
                engine=engine)


def _flush_group(fout, g, k, m, block_size, stream_id, group, frame_index,
                 block_id, parity_id, zeros, encode_group=rs_encode_group):
    n = len(group)
    # Zero-pad every data block to block_size at the END before encoding
    # parity (only the final block of the stream can be short).  The short
    # payload itself is written unpadded; the trailer's total_length is what
    # tells the decoder how many bytes of the last block are real.
    blocks = [b + bytes(block_size - len(b)) if len(b) < block_size else b
              for b in group]
    blocks += [zeros] * (k - n)
    for pos, payload in enumerate(group):
        fout.write(pack_header(FT_DATA, stream_id, frame_index + pos,
                               block_id + pos, len(payload), crc32(payload),
                               n))
        fout.write(payload)
    # The data frames do not depend on the parity: push them out before
    # computing it, so a decoder downstream can finish the previous group
    # while this one's parity is being worked out.
    getattr(fout, "flush", lambda: None)()
    parity = encode_group(blocks, block_size, g, m)
    for pos, payload in enumerate(parity):
        fout.write(pack_header(FT_PARITY, stream_id, frame_index + n + pos,
                               parity_id + pos, len(payload), crc32(payload),
                               n))
        fout.write(payload)


# ------------------------------------------------------------------ decoder ---


class DecodeReport:
    def __init__(self):
        self.groups = []
        self.unrecoverable_ranges = []
        self.blocks_repaired = 0
        self.blocks_unrecoverable = 0
        self.blocks_ok = 0
        self.blocks_seen = 0
        self.bytes_written = 0
        self.total_length = None
        self.sha256_expected = None
        self.sha256_match = None
        self.stream_id = None
        self.version = None
        self.k = self.m = self.block_size = None
        self.notes = []
        self.read_stats = {}
        self.trailer_seen = False

    @property
    def ok(self):
        """Nothing was lost.  Does not imply the digest was checked."""
        return (self.blocks_unrecoverable == 0
                and self.sha256_match is not False)

    @property
    def verified(self):
        """The trailer survived and the SHA-256 of the output matches it."""
        return self.trailer_seen and self.sha256_match is True

    def to_json(self, extra=None):
        obj = dict(ok=self.ok,
                    verified=self.verified,
                    stream_id=self.stream_id,
                    version=self.version,
                    k=self.k, m=self.m, block_size=self.block_size,
                   total_length=self.total_length,
                   bytes_written=self.bytes_written,
                   trailer_seen=self.trailer_seen,
                   sha256_match=self.sha256_match,
                   blocks_seen=self.blocks_seen,
                   blocks_ok=self.blocks_ok,
                   blocks_repaired=self.blocks_repaired,
                   blocks_unrecoverable=self.blocks_unrecoverable,
                   foreign_frames=getattr(self, "foreign_frames", 0),
                   groups=self.groups,
                   unrecoverable_ranges=self.unrecoverable_ranges,
                   notes=self.notes)
        if extra:
            obj.update(extra)
        return obj


class _DelayLine:
    """Holds back the last `cap` blocks so the trailer's total_length can trim
    the tail of the output (a final group whose length was never observed)."""

    def __init__(self, cap, sink):
        self.cap = cap
        self.sink = sink
        self.items = []

    def push(self, payload):
        self.items.append(payload)
        while len(self.items) > self.cap:
            self.sink(self.items.pop(0))

    def held(self):
        return sum(len(x) for x in self.items)

    def drain(self, trim_to=None):
        trimmed = 0
        if trim_to is not None:
            # Trim from the TAIL (the newest held block): the trailer's
            # total_length can only ever shorten the end of the stream.
            # Trimming the oldest held block would delete good data.
            excess = self.held() - trim_to
            while excess > 0 and self.items:
                last = self.items[-1]
                if excess >= len(last):
                    excess -= len(last)
                    trimmed += len(last)
                    self.items.pop()
                else:
                    trimmed += excess
                    self.items[-1] = last[:len(last) - excess]
                    excess = 0
        while self.items:
            self.sink(self.items.pop(0))
        return trimmed


class _GroupDecoder:
    def __init__(self, fout, rep, k, m, block_size, version=FORMAT_VERSION,
                 repair=rs_repair):
        self.fout = fout
        self.flush = getattr(fout, "flush", lambda: None)
        self.repair = repair
        self.rep = rep
        self.k, self.m, self.block_size = k, m, block_size
        self.g = generator_matrix(k, m, version)
        self.zeros = bytes(block_size)
        self.digest = hashlib.sha256()
        self.total = 0
        self.pushed = 0
        self.delay = _DelayLine(k + 1, self._sink)
        self.group_index = 0
        self.cur_group = None
        self.cur_data = {}
        self.cur_parity = {}
        self.cur_gdl = k

    def _sink(self, payload):
        self.digest.update(payload)
        self.fout.write(payload)
        self.total += len(payload)

    def _emit(self, payload):
        self.pushed += len(payload)
        self.delay.push(payload)

    def pad_missing_tail(self, total_length):
        """Groups after the last surviving frame: nothing to repair from, so
        the only honest thing is to emit zeros and say where they are."""
        missing = total_length - self.pushed
        if missing <= 0:
            return 0
        self.rep.notes.append(
            "%d byte(s) of the original stream are past offset %d with no "
            "frames at all to repair them from" % (missing, self.pushed))
        blocks = 0
        while self.pushed < total_length:
            group = self.pushed // (self.k * self.block_size)
            group_end = min(total_length, (group + 1) * self.k * self.block_size)
            group_blocks = 0
            while self.pushed < group_end:
                n = min(self.block_size, group_end - self.pushed)
                start = self.pushed
                ordinal = start // self.block_size
                self._emit(bytes(n))
                self.rep.blocks_unrecoverable += 1
                self.rep.unrecoverable_ranges.append(
                    dict(group=group, block=ordinal % self.k,
                         start=start, end=start + n))
                group_blocks += 1
            blocks += group_blocks
            self.rep.groups.append(
                dict(group=group, status="unrecoverable",
                     data_blocks=group_blocks, lost_data=group_blocks,
                     lost_parity=self.m, repaired=0,
                     note="trailing group with no frames at all"))
        return blocks

    def _note_range(self, group, pos):
        start = group * self.k * self.block_size + pos * self.block_size
        self.rep.unrecoverable_ranges.append(
            dict(group=group, block=pos, start=start,
                 end=start + self.block_size))

    def _record_group(self, status, gdl, lost_data, lost_parity, repaired=0):
        entry = dict(group=self.group_index, status=status, data_blocks=gdl,
                     lost_data=lost_data, lost_parity=lost_parity,
                     repaired=repaired)
        self.rep.groups.append(entry)
        return entry

    def zero_group(self):
        """A group of which no frame survived at all."""
        self._record_group("unrecoverable", self.k, self.k, self.m)
        self.rep.blocks_unrecoverable += self.k
        self.rep.notes.append("group %d: all %d data blocks are absent"
                              % (self.group_index, self.k))
        for pos in range(self.k):
            self._note_range(self.group_index, pos)
            self._emit(self.zeros)
        self.advance()

    def advance(self):
        self.flush()
        self.group_index += 1
        self.cur_group = None
        self.cur_data = {}
        self.cur_parity = {}
        self.cur_gdl = self.k

    def start_group(self, gidx, gdl):
        self.cur_group = gidx
        self.cur_gdl = gdl
        self.cur_data = {}
        self.cur_parity = {}

    def add_data(self, pos, payload):
        if payload is not None and pos < self.cur_gdl \
                and pos not in self.cur_data:
            self.cur_data[pos] = payload
            self.rep.blocks_seen += 1

    def add_parity(self, pos, payload):
        if payload is not None and pos < self.m and pos not in self.cur_parity:
            self.cur_parity[pos] = payload
            self.rep.blocks_seen += 1

    def finalize_group(self):
        gidx = self.cur_group
        gdl = self.cur_gdl
        known = self.cur_data
        lost = [p for p in range(gdl) if p not in known]
        live = self.cur_parity

        if lost and len(live) < len(lost):
            self._record_group("unrecoverable", gdl, len(lost),
                               self.m - len(live))
            self.rep.blocks_unrecoverable += len(lost)
            for pos in lost:
                self._note_range(gidx, pos)
            for pos in range(gdl):
                self._emit(known.get(pos, self.zeros))
            self.advance()
            return

        try:
            fixed = self.repair(known, lost, live, self.block_size, self.g,
                                self.m) if lost else {}
        except Unrecoverable:
            self._record_group("unrecoverable", gdl, len(lost),
                               self.m - len(live))
            self.rep.blocks_unrecoverable += len(lost)
            for pos in lost:
                self._note_range(gidx, pos)
            for pos in range(gdl):
                self._emit(known.get(pos, self.zeros))
            self.advance()
            return
        except TapefecError as exc:
            # A singular submatrix (only reachable on a version-1 stream
            # whose (k, m) is not MDS for this erasure pattern): the group
            # is unrecoverable, but decode must CONTINUE and report exit 1,
            # never abort with exit 2.
            self._record_group("unrecoverable", gdl, len(lost),
                               self.m - len(live))
            self.rep.notes.append("group %d: repair impossible: %s"
                                  % (gidx, exc))
            self.rep.blocks_unrecoverable += len(lost)
            for pos in lost:
                self._note_range(gidx, pos)
            for pos in range(gdl):
                self._emit(known.get(pos, self.zeros))
            self.advance()
            return

        if lost:
            self._record_group("repaired", gdl, len(lost),
                               self.m - len(live), repaired=len(lost))
            self.rep.blocks_repaired += len(lost)
        else:
            self._record_group("ok", gdl, 0, self.m - len(live))
        self.rep.blocks_ok += gdl - len(lost)
        for pos in range(gdl):
            self._emit(fixed[pos] if pos in fixed else known[pos])
        self.advance()

    def finish(self, total_length):
        # drain() receives the remaining held length, not the whole stream:
        # earlier blocks may already have passed through the delay line.
        remaining = (None if total_length is None else
                     max(0, total_length - self.total))
        trimmed = self.delay.drain(trim_to=remaining)
        if total_length is not None and self.total < total_length:
            self.rep.notes.append(
                "recovered %d bytes but the trailer says %d: %d bytes are "
                "missing and cannot be reconstructed"
                % (self.total, total_length, total_length - self.total))
        return trimmed


def decode_stream(fin, fout, report_foreign=True, engine=None):
    """Decode the FEC stream in `fin` into `fout`; returns a DecodeReport.

    Every recoverable byte is written.  Unrecoverable ranges are written as
    zeros so downstream tools see the right stream length; they are listed in
    the report.  `engine` picks the repair arithmetic (see resolve_engine);
    the output and the report do not depend on it.
    """
    repair = _repair_for(engine)
    rep = DecodeReport()
    bad_length = 0
    closed_early = closed_gdl = None
    reader = FrameReader(fin, ABSOLUTE_MAX_PAYLOAD)
    dec = None
    stream_id = None
    foreign = 0
    seen_header = False

    for head, payload in reader.frames():
        ftype = head["ftype"]

        if ftype == FT_HEADER:
            if seen_header:
                continue
            if payload is None or len(payload) != STREAM_HEADER_SIZE:
                raise TapefecError("stream header frame has a damaged payload")
            k, m, block_size, flags, _r = STREAM_HEADER_FMT.unpack(payload)
            if not 1 <= k <= 255 or not 1 <= m <= 255 or k + m > 256:
                raise TapefecError("stream header frame declares bad k/m")
            if block_size < 1:
                raise TapefecError("stream header frame declares a bad "
                                   "block_size")
            if flags:
                rep.notes.append("stream header flags = 0x%04x; unknown bits "
                                 "ignored" % flags)
            rep.k, rep.m, rep.block_size = k, m, block_size
            rep.version = head["version"]
            stream_id = head["stream_id"]
            rep.stream_id = stream_id
            seen_header = True
            reader.max_payload = max(block_size, STREAM_HEADER_SIZE,
                                     TRAILER_SIZE)
            dec = _GroupDecoder(fout, rep, k, m, block_size,
                                version=head["version"], repair=repair)
            continue

        if ftype == FT_TRAILER:
            if not seen_header:
                raise TapefecError("trailer frame before the stream header")
            if payload is not None and len(payload) == TRAILER_SIZE and \
                    TRAILER_FMT.unpack(payload)[0] <= \
                    max(head["frame_index"], reader.stats["frames"]) \
                    * dec.block_size:
                rep.total_length, rep.sha256_expected = \
                    TRAILER_FMT.unpack(payload)
                rep.trailer_seen = True
            else:
                rep.notes.append("trailer frame is missing or damaged: the "
                                 "length and digest cannot be checked")
            break

        if not seen_header:
            continue      # leading junk before the stream header

        if head["stream_id"] != stream_id:
            foreign += 1
            continue      # a wrong-stream frame must never fill a real slot

        if head["block_id"] >= head["frame_index"]:
            continue      # every block occupies a frame, so this id is impossible
        if payload is not None and (
                len(payload) > dec.block_size if ftype == FT_DATA
                else len(payload) != dec.block_size):
            # Parity is always exactly block_size and data never longer: a
            # CRC-valid payload of any other length cannot be a codeword
            # symbol, so it is an erasure rather than repair input.
            bad_length += 1
            payload = None
        if not 0 < head["group_data_len"] <= dec.k:
            head["group_data_len"] = 0      # out of range: assume a full group

        if ftype == FT_DATA:
            ordinal = head["block_id"]
            gidx = ordinal // dec.k
        else:
            ordinal = head["block_id"]
            gidx = ordinal // dec.m

        if gidx == closed_early and dec.cur_group is None:
            # A late frame of a group that was complete and already emitted:
            # report exactly what the open group would have, then drop it.
            gdl = head["group_data_len"] or dec.k
            if gdl != closed_gdl:
                rep.notes.append("group %d: inconsistent group_data_len (%d "
                                 "vs %d); keeping %d" % (gidx, gdl, closed_gdl,
                                                         closed_gdl))
            continue
        if gidx < dec.group_index:
            rep.notes.append("ignoring frame for group %d, already past it"
                             % gidx)
            continue

        if dec.cur_group is None:
            while dec.group_index < gidx:
                dec.zero_group()
            dec.start_group(gidx, head["group_data_len"] or dec.k)
        elif gidx != dec.cur_group:
            dec.finalize_group()
            while dec.group_index < gidx:
                dec.zero_group()
            dec.start_group(gidx, head["group_data_len"] or dec.k)

        gdl = head["group_data_len"] or dec.k
        if gdl != dec.cur_gdl and not dec.cur_data and not dec.cur_parity:
            dec.cur_gdl = gdl
        elif gdl != dec.cur_gdl:
            rep.notes.append("group %d: inconsistent group_data_len (%d vs "
                             "%d); keeping %d" % (gidx, gdl, dec.cur_gdl,
                                                  dec.cur_gdl))

        if ftype == FT_DATA:
            dec.add_data(ordinal - gidx * dec.k, payload)
        else:
            dec.add_parity(ordinal - gidx * dec.m, payload)
        if len(dec.cur_data) == dec.cur_gdl and len(dec.cur_parity) == dec.m:
            # Every frame of the group is in and intact, so nothing later
            # can change it: emit it now instead of when the next group's
            # first frame arrives, which lets a pipe's two ends overlap.
            closed_early, closed_gdl = gidx, dec.cur_gdl
            dec.finalize_group()

    rep.read_stats = dict(reader.stats)
    if bad_length:
        rep.notes.append("%d frame(s) carried a payload length impossible "
                         "for block_size %d and were treated as damaged"
                         % (bad_length, dec.block_size))

    if not seen_header:
        raise TapefecError("no stream header frame found (not a tapefec "
                           "stream, or the header frame is gone)")

    if dec.cur_group is not None:
        dec.finalize_group()
    if rep.trailer_seen:
        dec.pad_missing_tail(rep.total_length)
        # Only a surviving trailer supplies a known EOF. Without one, retain
        # the block-sized estimates rather than inventing a shorter length.
        rep.unrecoverable_ranges = [
            dict(entry, end=min(entry["end"], rep.total_length))
            for entry in rep.unrecoverable_ranges
            if entry["start"] < rep.total_length
        ]
    elif dec.pushed:
        rep.notes.append(
            "stream is not terminated by a trailer frame: %d byte(s) were "
            "written but the original length is unknown"
            % dec.pushed)
    trimmed = dec.finish(rep.total_length if rep.trailer_seen else None)
    if trimmed:
        rep.notes.append("trimmed %d trailing bytes to match the trailer "
                         "length" % trimmed)

    rep.bytes_written = dec.total
    rep.foreign_frames = foreign
    if report_foreign and foreign:
        rep.notes.append("%d frame(s) carried a different stream id and were "
                         "ignored" % foreign)
    if rep.sha256_expected is not None:
        rep.sha256_match = (dec.digest.digest() == rep.sha256_expected)
    return rep


def scan_stream(fin, on_frame=None):
    """Read a FEC stream once, without decoding it.

    Returns (info, counts, stats, damaged).  `on_frame(head, payload)`, when
    given, is called for every frame as it is read -- use it to stream a
    table out instead of holding one record per frame in memory.
    """
    reader = FrameReader(fin, ABSOLUTE_MAX_PAYLOAD)
    info = {}
    counts = {}
    damaged = 0
    for head, payload in reader.frames():
        ftype = head["ftype"]
        counts[ftype] = counts.get(ftype, 0) + 1
        if payload is None:
            damaged += 1
        if ftype == FT_HEADER and payload is not None and \
                len(payload) == STREAM_HEADER_SIZE:
            k, m, bs, flags, _r = STREAM_HEADER_FMT.unpack(payload)
            info.update(k=k, m=m, block_size=bs, flags=flags)
        if ftype == FT_TRAILER and payload is not None and \
                len(payload) == TRAILER_SIZE:
            tl, sha = TRAILER_FMT.unpack(payload)
            info.update(total_length=tl, sha256=sha.hex())
        if on_frame is not None:
            on_frame(head, payload)
    return info, counts, dict(reader.stats), damaged


def inspect_stream(fin):
    """Read a FEC stream and describe it without decoding.  Returns
    (frames, info, counts, stats, damaged) with one small dict per frame."""
    frames = []

    def collect(head, payload):
        frames.append(dict(index=head["frame_index"],
                           type=FT_NAMES[head["ftype"]],
                           stream_id=head["stream_id"],
                           block_id=head["block_id"],
                           payload_len=head["payload_len"],
                           group_data_len=head["group_data_len"],
                           payload_crc="%08x" % head["payload_crc"],
                           payload_ok=payload is not None))

    info, counts, stats, damaged = scan_stream(fin, on_frame=collect)
    return frames, info, counts, stats, damaged


# ------------------------------------------------------------------- damage ---


def _read_exact(fin, n):
    """Read exactly n bytes unless EOF comes first."""
    parts, got = [], 0
    while got < n:
        part = fin.read(n - got)
        if not part:
            break
        parts.append(part)
        got += len(part)
    return b"".join(parts)


def damage_stream(fin, fout, per_group=None, rate=0.0, mode="mixed", seed=0):
    """Copy an undamaged FEC stream from `fin` to `fout`, simulating a bad
    tape on the way: in every parity group, `per_group` randomly chosen data
    or parity frames (or each frame with probability `rate`) are dropped or
    have 1..8 payload bytes flipped.  The stream header and trailer frames
    pass through untouched, so what the decoder can or cannot repair is down
    to the parity alone.

    Streams group by group: memory is one group of frames, and each group is
    written (and flushed) as soon as its last frame, or the next group's
    first, has been read.  Deterministic for
    a given `seed`.  Returns a summary dict.
    """
    if mode not in ("drop", "corrupt", "mixed"):
        raise TapefecError("damage mode must be drop, corrupt or mixed")
    if per_group is not None and per_group < 0:
        raise TapefecError("--per-group must be >= 0")
    if not 0.0 <= rate <= 1.0:
        raise TapefecError("--rate must be between 0 and 1")
    rng = random.Random(seed)
    flush = getattr(fout, "flush", lambda: None)
    summary = dict(frames=0, groups=0, dropped=0, corrupted=0)
    k = m = None
    max_payload = ABSOLUTE_MAX_PAYLOAD
    group, group_key = [], None

    def emit_group():
        if not group:
            return
        if per_group is not None:
            hit = set(rng.sample(range(len(group)),
                                 min(per_group, len(group))))
        else:
            hit = {i for i in range(len(group)) if rng.random() < rate}
        for i, (raw, plen) in enumerate(group):
            if i in hit:
                how = mode if mode != "mixed" else rng.choice(
                    ("drop", "corrupt"))
                if how == "drop" or plen == 0:
                    summary["dropped"] += 1
                    continue
                raw = bytearray(raw)
                for off in rng.sample(range(plen), min(plen,
                                                       rng.randint(1, 8))):
                    raw[HEADER_SIZE + off] ^= rng.randint(1, 255)
                summary["corrupted"] += 1
            fout.write(raw)
        summary["groups"] += 1
        del group[:]
        flush()

    while True:
        raw = _read_exact(fin, HEADER_SIZE)
        if not raw:
            break
        head = unpack_header(raw)
        if head is None or head["payload_len"] > max_payload:
            raise TapefecError("damage needs an undamaged tapefec stream; "
                               "bad frame header at frame %d"
                               % summary["frames"])
        payload = _read_exact(fin, head["payload_len"])
        if len(payload) < head["payload_len"]:
            raise TapefecError("damage input is truncated inside frame %d"
                               % summary["frames"])
        summary["frames"] += 1
        ftype = head["ftype"]
        if ftype == FT_HEADER:
            if len(payload) != STREAM_HEADER_SIZE:
                raise TapefecError("damage: bad stream header frame")
            k, m, block_size, _f, _r = STREAM_HEADER_FMT.unpack(payload)
            if not k or not m:
                raise TapefecError("damage: stream header declares bad k/m")
            max_payload = max(block_size, STREAM_HEADER_SIZE, TRAILER_SIZE)
        if ftype in (FT_DATA, FT_PARITY):
            if k is None:
                raise TapefecError("damage: data before the stream header")
            key = head["block_id"] // (k if ftype == FT_DATA else m)
            if key != group_key:
                emit_group()
                group_key = key
            group.append((raw + payload, head["payload_len"]))
            if len(group) == (head["group_data_len"] or k) + m:
                emit_group()       # complete: no need to wait for the next
            continue
        emit_group()
        fout.write(raw + payload)
        flush()
    emit_group()
    return summary


# ---------------------------------------------------------------------- CLI ---


def _build_parser():
    p = argparse.ArgumentParser(
        prog="tapefec",
        description="Streaming forward-error-correction framer for "
                    "tar-to-tape. Python 3 standard library only.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  tar -c -C /srv photos | tapefec encode | dd of=photos.tapefec bs=1M
  tapefec decode < photos.tapefec > photos.tar
  tapefec inspect < photos.tapefec
  tapefec encode < x | tapefec damage --per-group 4 | tapefec decode > y

Exit status:
  0  every byte recovered and the SHA-256 in the trailer matched
  1  something was unrecoverable, or the trailer is gone so nothing could be
     verified (use --strict to also fail when any repair was needed)
  2  not a tapefec stream, or bad arguments

Write to a FILE, never to a tape device: tapefec cannot re-read what it wrote.
""")
    p.add_argument("--version", action="version",
                   version="tapefec %s (format v%d)"
                           % (__version__, FORMAT_VERSION))
    p.add_argument("--engine", choices=ENGINES, default=None,
                   help="Reed-Solomon arithmetic: pure Python, numpy, or "
                        "auto (numpy if importable). Output is identical. "
                        "Default: $TAPEFEC_ENGINE, else auto")
    sub = p.add_subparsers(dest="command",
                           metavar="{encode,decode,inspect,damage}")

    e = sub.add_parser("encode", help="read a stream, write a FEC stream")
    e.add_argument("-k", type=int, default=DEFAULT_K,
                   help="data blocks per parity group (default %(default)s)")
    e.add_argument("-m", type=int, default=DEFAULT_M,
                   help="parity blocks per group; up to m blocks per group "
                        "can be lost and recovered (default %(default)s)")
    e.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE,
                   dest="block_size",
                   help="data block size in bytes (default %(default)s)")
    e.add_argument("--stream-id", type=int, default=0,
                   help="16-bit stream id stamped in every frame "
                        "(default %(default)s)")

    d = sub.add_parser("decode", help="read a FEC stream, write the original")
    d.add_argument("--json", action="store_true",
                   help="stderr gets only the JSON report")
    d.add_argument("--text", action="store_true",
                   help="stderr gets human-readable per-group lines too")
    d.add_argument("--quiet", "-q", action="store_true",
                   help="suppress the report when everything recovered")
    d.add_argument("--strict", action="store_true",
                   help="exit non-zero unless the digest matches and nothing "
                        "needed repair")

    i = sub.add_parser("inspect", help="print the frame map of a FEC stream")
    i.add_argument("--json", action="store_true",
                   help="emit the frame map as JSON")

    dm = sub.add_parser("damage",
                        help="simulate bad tape blocks in a FEC stream "
                             "(for recovery demos and restore drills)")
    how = dm.add_mutually_exclusive_group()
    how.add_argument("--per-group", type=int, default=None, metavar="N",
                     help="damage exactly N data/parity frames in every "
                          "group (default 1)")
    how.add_argument("--rate", type=float, default=None, metavar="P",
                     help="damage each data/parity frame with probability P")
    dm.add_argument("--mode", choices=("drop", "corrupt", "mixed"),
                    default="mixed",
                    help="delete frames, flip payload bytes, or a 50/50 mix "
                         "(default %(default)s)")
    dm.add_argument("--seed", type=int, default=0,
                    help="random seed (default %(default)s)")
    return p


def cmd_encode(args, fin, fout, err):
    info = encode_stream(fin, fout, k=args.k, m=args.m,
                         block_size=args.block_size,
                         stream_id=args.stream_id)
    err.write("tapefec: encoded %d bytes into %d data + %d parity blocks "
              "(k=%d m=%d block=%d stream_id=%d, %d frames, engine=%s)\n"
              % (info["total_length"], info["data_blocks"],
                 info["parity_blocks"], info["k"], info["m"],
                 info["block_size"], info["stream_id"], info["frame_index"],
                 info["engine"]))
    return 0


def cmd_damage(args, fin, fout, err):
    per_group = args.per_group
    if per_group is None and args.rate is None:
        per_group = 1
    summary = damage_stream(fin, fout, per_group=per_group,
                            rate=args.rate or 0.0, mode=args.mode,
                            seed=args.seed)
    err.write("tapefec: damage: %s\n" % json.dumps(summary, sort_keys=True))
    return 0


def cmd_decode(args, fin, fout, err):
    try:
        rep = decode_stream(fin, fout)
    except TapefecError as exc:
        err.write("tapefec: %s\n" % exc)
        return 2
    # Compute status first. A verified repair may be quiet even when strict
    # mode rejects it, but loss, failed integrity and missing trailers must
    # still produce a report.
    if rep.blocks_unrecoverable or rep.sha256_match is False:
        rc = 1
    elif not rep.trailer_seen:
        rc = 1          # unterminated stream: nothing was lost but nothing
                        # was verified either, so do not report success
    elif args.strict and rep.blocks_repaired:
        rc = 1
    else:
        rc = 0
    if args.text and not args.json and not (args.quiet and rc == 0):
        err.write("tapefec: %d group(s); %d blocks ok, %d repaired, "
                  "%d unrecoverable; read %d frames, discarded %d garbage "
                  "byte(s), %d resync(s), %d bad payload(s)\n"
                  % (len(rep.groups), rep.blocks_ok, rep.blocks_repaired,
                     rep.blocks_unrecoverable, rep.read_stats["frames"],
                     rep.read_stats["garbage_bytes"],
                     rep.read_stats["resyncs"],
                     rep.read_stats["bad_payload"]))
        for ge in rep.groups:
            if ge["status"] != "ok":
                err.write("tapefec: group %d: %s (repaired=%d lost_data=%d "
                          "lost_parity=%d)\n"
                          % (ge["group"], ge["status"], ge["repaired"],
                             ge["lost_data"], ge["lost_parity"]))
        # Notes are part of the JSON report (the "notes" array); the
        # human-readable "tapefec: note:" lines are a --text convenience.
        for note in rep.notes:
            err.write("tapefec: note: %s\n" % note)
    # --quiet suppresses the report on a clean (status 0) run; on a failure
    # it is still emitted, except for --quiet --strict where a repaired group
    # is the only cause of failure (still exit 1, but no report).
    strict_repair_only = (args.strict and rep.blocks_repaired
                          and rep.blocks_unrecoverable == 0 and rep.verified)
    if args.quiet and (rc == 0 or strict_repair_only):
        pass
    else:
        err.write(json.dumps(rep.to_json({"read": rep.read_stats}),
                             sort_keys=True) + "\n")
    return rc


def cmd_inspect(args, fin, fout, err):
    if args.json:
        frames, info, counts, stats, damaged = inspect_stream(fin)
        empty_input = (stats.get("frames", 0) == 0
                       and stats.get("garbage_bytes", 0) == 0)
        if FT_HEADER not in counts and not empty_input:
            err.write("tapefec: not a tapefec stream (no stream-header frame "
                      "was seen)\n")
            return 2
        json.dump(dict(info=info,
                       counts={FT_NAMES[k]: v for k, v in
                               sorted(counts.items())},
                       read=stats, damaged_payloads=damaged, frames=frames),
                  fout, indent=2, sort_keys=True)
        fout.write("\n")
        return 0
    # Text mode: the per-frame table is streamed out as one small string
    # per frame (decode and encode are the bounded-memory paths; inspect
    # scales with the frame count by design).
    rows = []

    def on_frame(head, payload):
        rows.append("%-8d %-8s %-6d %-12d %-12d %s\n"
                    % (head["frame_index"], FT_NAMES[head["ftype"]],
                       head["stream_id"], head["block_id"],
                       head["payload_len"], payload is not None))

    info, counts, stats, damaged = scan_stream(fin, on_frame=on_frame)
    empty_input = (stats.get("frames", 0) == 0
                   and stats.get("garbage_bytes", 0) == 0)
    if FT_HEADER not in counts and not empty_input:
        err.write("tapefec: not a tapefec stream (no stream-header frame "
                  "was seen)\n")
        return 2
    fout.write("stream: k=%s m=%s block_size=%s flags=%s total_length=%s\n"
               % (info.get("k", "?"), info.get("m", "?"),
                  info.get("block_size", "?"), info.get("flags", "?"),
                  info.get("total_length", "?")))
    fout.write("sha256(original): %s\n" % info.get("sha256", "?"))
    by_type = ", ".join("%s=%d" % (FT_NAMES[t], counts[t])
                        for t in (FT_HEADER, FT_DATA, FT_PARITY, FT_TRAILER)
                        if t in counts)
    fout.write("frames: %d (%s)  damaged payloads: %d\n"
               % (sum(counts.values()), by_type or "none", damaged))
    fout.write("read stats: %s\n" % json.dumps(stats, sort_keys=True))
    fout.write("\n%-8s %-8s %-6s %-12s %-12s %s\n"
               % ("frame", "type", "stream", "block_id", "payload", "ok"))
    fout.writelines(rows)
    return 0


def widen_pipe(fh, size=1 << 20):
    """Best effort: grow a Linux pipe's buffer from 64 KiB to `size`.

    Each stage of `encode | damage | decode` works a whole group at a time.
    With a 64 KiB pipe the writer blocks a few frames into every group until
    the reader catches up, so the stages take turns instead of overlapping.
    A pipe that holds most of a group lets them run concurrently.  Anything
    that is not a Linux pipe, or a size above /proc/sys/fs/pipe-max-size, is
    left alone."""
    try:
        import fcntl
        fcntl.fcntl(fh.fileno(), fcntl.F_SETPIPE_SZ, size)
    except (ImportError, AttributeError, OSError, ValueError):
        pass


def check_not_a_tty(fout):
    """True when `fout` is a terminal.  Writing a binary FEC stream to a
    terminal would garble it, so encode/decode refuse; the hint tells the
    user to redirect.  Returns False for anything without isatty()."""
    try:
        return bool(fout.isatty())
    except (AttributeError, ValueError):
        return False


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2
    err = sys.stderr
    # tapefec makes no BLAS calls, but numpy's BLAS spins up one thread per
    # core at import, which costs more CPU than encoding a few MB.  Only
    # defaults: a value the user exported wins.
    for var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(var, "1")
    try:
        set_engine(args.engine or os.environ.get("TAPEFEC_ENGINE") or "auto")
    except TapefecError as exc:
        err.write("tapefec: %s\n" % exc)
        return 2
    binary_output = args.command in ("encode", "decode", "damage")
    if binary_output and check_not_a_tty(sys.stdout):
        err.write("tapefec: refusing to write a binary stream to a terminal; "
                  "redirect stdout to a file or pipe\n")
        return 2
    fin = sys.stdin.buffer
    fout = sys.stdout.buffer if binary_output else None
    if binary_output:
        widen_pipe(fin)
        widen_pipe(fout)
    try:
        if args.command == "encode":
            rc = cmd_encode(args, fin, fout, err)
        elif args.command == "decode":
            rc = cmd_decode(args, fin, fout, err)
        elif args.command == "damage":
            rc = cmd_damage(args, fin, fout, err)
        else:
            rc = cmd_inspect(args, fin, sys.stdout, err)
        if binary_output:
            fout.flush()
        sys.stdout.flush()
        return rc
    except TapefecError as exc:
        err.write("tapefec: %s\n" % exc)
        return 2
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        err.write("tapefec: interrupted\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
