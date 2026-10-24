#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Fuzz harness for tapefec's framer and decoder.  Standard library only.

Each case encodes a random input with random (k, m, block_size, version,
stream_id), then mangles the encoded bytes with a random stack of mutators --
bit flips, byte runs, insertions and deletions, dropped, duplicated and moved
frames, junk containing the magic, truncation, frames spliced in from a stream
with another stream_id -- and decodes the result.  The oracle:

  1. decode either raises TapefecError (exit 2: "not a stream") or returns
     a report; any other exception is a bug;
  2. a report that would exit 0 means the output IS the original input;
  3. every byte outside a reported-unrecoverable range is the original
     byte (these mutators never forge a CRC-valid frame of our stream, so
     the decoder has no excuse for believing wrong data);
  4. the output never runs past the trailer's length, nor past one block
     per frame slot the input's CRC-valid frames claim (plus one group).

--forge adds one CRC-valid lie per case (a re-sealed header field, or frames
of another stream with the SAME stream_id).  The decoder cannot always tell
those from real frames, so only oracles 1, 2 and the trailer half of 4 apply.

    nice -n 19 python3 fuzz.py                        # 2000 cases
    nice -n 19 python3 fuzz.py --cases 50000 --seed 7
    nice -n 19 python3 fuzz.py --seconds 600          # time-boxed
    python3 fuzz.py --replay 1234567 [--forge]        # one case, verbose

A failing case prints its seed; --replay (with the same --forge) reproduces it.
"""

import argparse
import io
import os
import random
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tapefec as T  # noqa: E402


# ----------------------------------------------------------------- mutators ---


def _frame_spans(enc):
    spans, p = [], 0
    while p + T.HEADER_SIZE <= len(enc):
        head = T.unpack_header(enc[p:p + T.HEADER_SIZE])
        if head is None:
            break
        n = T.HEADER_SIZE + head["payload_len"]
        spans.append((p, p + n, head))
        p += n
    return spans


def mut_bitflip(rng, buf, spans):
    for _ in range(rng.randint(1, 4)):
        if buf:
            i = rng.randrange(len(buf))
            buf[i] ^= 1 << rng.randrange(8)
    return "bitflip"


def mut_byte_run(rng, buf, spans):
    if buf:
        i = rng.randrange(len(buf))
        n = rng.randint(1, 64)
        buf[i:i + n] = bytes(rng.getrandbits(8) for _ in range(len(buf[i:i + n])))
    return "byte_run"


def mut_insert(rng, buf, spans):
    i = rng.randint(0, len(buf))
    buf[i:i] = rng.randbytes(rng.randint(1, 80))
    return "insert"


def mut_delete(rng, buf, spans):
    if buf:
        i = rng.randrange(len(buf))
        del buf[i:i + rng.randint(1, 80)]
    return "delete"


def mut_drop_frame(rng, buf, spans):
    if spans:
        s, e, _h = rng.choice(spans)
        if e <= len(buf):
            del buf[s:e]
    return "drop_frame"


def mut_dup_frame(rng, buf, spans):
    if spans:
        s, e, _h = rng.choice(spans)
        if e <= len(buf):
            at = rng.choice(spans)[0]
            buf[at:at] = buf[s:e]
    return "dup_frame"


def mut_move_frame(rng, buf, spans):
    """Move a frame elsewhere (out of order, possibly across groups)."""
    if spans:
        s, e, _h = rng.choice(spans)
        if e <= len(buf):
            frame = bytes(buf[s:e])
            del buf[s:e]
            at = min(rng.choice(spans)[0], len(buf))
            buf[at:at] = frame
    return "move_frame"


def mut_magic_junk(rng, buf, spans):
    """Junk that contains the resync marker, to tempt the scanner."""
    i = rng.randint(0, len(buf))
    junk = rng.randbytes(rng.randint(0, 20)) + T.MAGIC + \
        rng.randbytes(rng.randint(0, 50))
    buf[i:i] = junk
    return "magic_junk"


def mut_truncate(rng, buf, spans):
    if buf:
        del buf[rng.randint(0, len(buf) - 1):]
    return "truncate"


def _reframe(head, payload):
    return T.pack_header(head["ftype"], head["stream_id"], head["frame_index"],
                         head["block_id"], len(payload), T.crc32(payload),
                         head["group_data_len"], head["reserved"],
                         version=head["version"]) + payload


def mut_forge_field(rng, buf, spans):
    """Rewrite one header field of a real frame and re-seal its CRCs: an
    undetectable corruption, or a frame some other encoder wrote."""
    if not spans:
        return "forge_field"
    s, e, head = rng.choice(spans)
    if e > len(buf):
        return "forge_field"
    head = dict(head)
    payload = bytes(buf[s + T.HEADER_SIZE:e])
    field = rng.choice(["ftype", "block_id", "frame_index", "group_data_len",
                        "payload_len", "version"])
    if field == "ftype":
        head["ftype"] = rng.randrange(4)
    elif field == "version":
        head["version"] = rng.choice(T.SUPPORTED_VERSIONS)
    elif field == "payload_len":
        n = rng.choice([0, 1, len(payload) - 1, len(payload) + 1,
                        rng.randint(0, 3 * len(payload) + 64)])
        payload = (payload + rng.randbytes(max(0, n)))[:max(0, n)]
    else:
        head[field] = rng.choice([0, 1, head[field] - 1, head[field] + 1,
                                  rng.getrandbits(32)]) & 0xFFFFFFFF
    buf[s:e] = _reframe(head, payload)
    return "forge_" + field


# Originals of the streams mut_foreign_stream splices in, for the oracle: a
# decoder that lost our stream header and adopted the foreign one has decoded
# a real, complete stream, and exit 0 with ITS bytes is the right answer.
FOREIGN_ORIGINALS = []
# Same for mut_other_stream, keyed by stream id: if our header is lost and the
# decoder adopts the other stream, the full oracle applies against ITS bytes.
OTHER_ORIGINALS = {}
# Parameters of the case being built, for mutators that need them (the spans
# they get describe the clean stream, not the already-mutated buffer).
CASE = {}


def mut_foreign_stream(rng, buf, spans):
    """Splice in frames of ANOTHER stream with the same stream_id -- e.g.
    two archives encoded with the CLI default --stream-id 0 on one tape."""
    if not spans:
        return "foreign"
    sid = spans[0][2]["stream_id"]
    out = io.BytesIO()
    other_orig = rng.randbytes(rng.randint(0, 3000))
    FOREIGN_ORIGINALS.append(other_orig)
    T.encode_stream(io.BytesIO(other_orig), out,
                    k=rng.randint(1, 12), m=rng.randint(1, 6),
                    block_size=rng.choice([1, 2, 7, 16, 64, 1024]),
                    stream_id=sid, version=rng.choice(T.SUPPORTED_VERSIONS))
    other = out.getvalue()
    ospans = _frame_spans(other)
    for _ in range(rng.randint(1, 4)):
        s, e, _h = rng.choice(ospans)
        at = min(rng.choice(spans)[0], len(buf))
        buf[at:at] = other[s:e]
    return "foreign"


def mut_other_stream(rng, buf, spans):
    """Splice in whole frames of a stream with a DIFFERENT stream_id, same
    k/m/block_size -- the case stream ids exist for."""
    if not spans:
        return "other_stream"
    p = CASE
    stream = io.BytesIO()
    sid = (p["stream_id"] + rng.randint(1, 0xFFFF)) & 0xFFFF   # never ours
    other_orig = rng.randbytes(rng.randint(0, 3 * p["k"] * p["block_size"]))
    OTHER_ORIGINALS[sid] = other_orig
    T.encode_stream(io.BytesIO(other_orig), stream, k=p["k"], m=p["m"],
                    block_size=p["block_size"], stream_id=sid,
                    version=p["version"])
    other = stream.getvalue()
    ospans = _frame_spans(other)
    for _ in range(rng.randint(1, 6)):
        s, e, _h = rng.choice(ospans)
        at = min(rng.choice(spans)[0], len(buf))
        buf[at:at] = other[s:e]
    return "other_stream"


MUTATORS = [mut_bitflip, mut_byte_run, mut_insert, mut_delete,
            mut_drop_frame, mut_dup_frame, mut_move_frame, mut_magic_junk,
            mut_truncate, mut_other_stream]
WEIGHTS = [6, 3, 3, 3, 5, 2, 2, 2, 1, 2]
FORGERS = [mut_forge_field, mut_foreign_stream]


# ------------------------------------------------------------------- oracle ---


class Failure(AssertionError):
    pass


def decode_rc(rep):
    if rep.blocks_unrecoverable or rep.sha256_match is False:
        return 1
    if not rep.trailer_seen:
        return 1
    return 0


def check_no_wrong_bytes(rep, orig, out):
    holes = sorted((r["start"], r["end"]) for r in rep.unrecoverable_ranges)
    pos = 0
    n = min(len(orig), len(out))
    for start, end in holes + [(n, n)]:
        start = min(start, n)
        if start > pos and out[pos:start] != orig[pos:start]:
            off = next(i for i in range(pos, start) if out[i] != orig[i])
            raise Failure("wrong byte at offset %d outside every reported "
                          "unrecoverable range" % off)
        pos = max(pos, end)


def make_case(seed, forge=False):
    rng = random.Random(seed)
    k = rng.randint(1, 12)
    m = rng.randint(1, 6)
    block_size = rng.choice([1, 2, 3, 7, 16, 64, 255, 1024])
    length = rng.choice([0, 1, block_size - 1, block_size, block_size + 1,
                         rng.randint(0, 5 * k * block_size)])
    length = max(0, length)
    version = rng.choice([2, 2, 2, 1])
    stream_id = rng.randint(0, 0xFFFF)
    orig = rng.randbytes(length)
    out = io.BytesIO()
    T.encode_stream(io.BytesIO(orig), out, k=k, m=m, block_size=block_size,
                    stream_id=stream_id, version=version)
    CASE.clear()
    CASE.update(k=k, m=m, block_size=block_size, version=version,
                stream_id=stream_id)
    enc = out.getvalue()
    spans = _frame_spans(enc)
    buf = bytearray(enc)
    applied = []
    for _ in range(rng.randint(1, 5)):
        mut = rng.choices(MUTATORS, WEIGHTS)[0]
        applied.append(mut(rng, buf, spans))
    if forge:
        applied.append(rng.choice(FORGERS)(rng, buf, spans))
    params = dict(k=k, m=m, block_size=block_size, version=version,
                  length=length)
    return orig, bytes(buf), params, applied


def run_case(seed, forge=False):
    """Run one case.  Returns None on success or a description on failure.

    With `forge`, the case also gets one CRC-valid lie (see FORGERS).  The
    decoder cannot always tell such a frame from a real one, so oracle 3 is
    waived; it must still never crash and never exit 0 with wrong output.
    """
    del FOREIGN_ORIGINALS[:]
    OTHER_ORIGINALS.clear()
    orig, mangled, params, applied = make_case(seed, forge)
    ours = CASE["stream_id"]
    out = io.BytesIO()
    try:
        rep = T.decode_stream(io.BytesIO(mangled), out)
    except T.TapefecError:
        return None                      # exit 2: refused, loudly
    except Exception:                    # noqa: BLE001 -- that IS the bug
        return ("crash %s %s\n%s" % (params, applied, traceback.format_exc()))
    got = out.getvalue()
    if rep.stream_id != ours:
        # Our header was lost and the decoder adopted a spliced-in stream.
        if rep.stream_id not in OTHER_ORIGINALS:
            return "decoded unknown stream id %d %s %s" % (
                rep.stream_id, params, applied)
        orig = OTHER_ORIGINALS[rep.stream_id]
    try:
        if decode_rc(rep) == 0 and got != orig and \
                got not in FOREIGN_ORIGINALS:
            raise Failure("exit 0 but output differs from the original")
        if rep.trailer_seen and rep.total_length == len(orig) \
                and len(got) > len(orig):
            raise Failure("wrote %d bytes past the trailer length"
                          % (len(got) - len(orig)))
        if not forge:
            # Every block occupies a frame, so output never needs more than
            # one block per frame slot that some CRC-valid frame of the
            # adopted stream claims, plus the rest of that frame's group.
            frames = T.inspect_stream(io.BytesIO(mangled))[0]
            slots = max([f["index"] for f in frames
                         if f["stream_id"] == rep.stream_id] + [0]) \
                + 1 + rep.k
            if len(got) > slots * rep.block_size:
                raise Failure("emitted %d bytes but the input has at most %d "
                              "frame slots" % (len(got), slots))
            check_no_wrong_bytes(rep, orig, got)
    except Failure as exc:
        return "%s %s %s; notes=%s" % (exc, params, applied, rep.notes)
    return None


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--cases", type=int, default=2000)
    p.add_argument("--seconds", type=float, default=None,
                   help="run until this much time has passed instead")
    p.add_argument("--seed", type=int, default=None,
                   help="base seed (default: random)")
    p.add_argument("--replay", type=int, default=None,
                   help="run exactly this case seed and print it")
    p.add_argument("--forge", action="store_true",
                   help="also inject CRC-valid lies (forged header fields, "
                        "frames from another stream with the same id)")
    p.add_argument("--keep-going", action="store_true",
                   help="report every failure instead of stopping at one")
    args = p.parse_args(argv)

    if args.replay is not None:
        orig, mangled, params, applied = make_case(args.replay, args.forge)
        print("params:", params, "mutators:", applied,
              "encoded->mangled bytes:", len(mangled))
        res = run_case(args.replay, args.forge)
        print("OK" if res is None else "FAIL: " + res)
        return 0 if res is None else 1

    base = args.seed if args.seed is not None else random.SystemRandom() \
        .randrange(1 << 32)
    print("tapefec fuzz: base seed %d" % base, flush=True)
    t0 = time.monotonic()
    failures = 0
    n = 0
    while True:
        if args.seconds is not None:
            if time.monotonic() - t0 >= args.seconds:
                break
        elif n >= args.cases:
            break
        seed = (base * 1000003 + n) & 0xFFFFFFFFFFFF
        res = run_case(seed, args.forge)
        n += 1
        if res is not None:
            failures += 1
            print("FAIL seed=%d: %s" % (seed, res), flush=True)
            if not args.keep_going:
                break
    print("tapefec fuzz: %d case(s), %d failure(s), %.1fs"
          % (n, failures, time.monotonic() - t0))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
