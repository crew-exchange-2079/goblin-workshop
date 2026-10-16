#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Torture test for tapefec.

200 random trials over one 20 MB random input, with random frame-level damage
at rates 0 %, 1 %, 5 % and 15 %.  Reports, per rate: trials, fully recovered,
partially (but completely) recovered, unrecoverable -- and confirms that the
SHA-256 matches whenever the decoder says ok.

    nice -n 19 python3 torture.py                 # defaults: 200 trials, 20 MB
    nice -n 19 python3 torture.py --trials 20 --mb 2 --seed 1

Damage model
------------
A tape loses whole blocks, so the unit of damage here is the FRAME, not the
byte.  Each frame of the encoded stream independently suffers damage with
probability `rate`; the damage is either

  * deleted outright (the frame's bytes vanish from the stream), or
  * the payload is corrupted (1..8 random bytes flipped inside the payload, so
    the payload CRC catches it and the frame becomes an erasure),

chosen 50/50.  The stream header and trailer frames are damaged on the same
terms as any other frame, so a trial can also lose its own length/digest.

"Rates" are therefore fractions of FRAMES, which for k=20,m=4 means a rate of
p leaves a fraction P(X > m) of groups unrecoverable where X ~ Binomial(k+m, p).
At 15 % that is a ~43 % per-group failure rate, which is the point: it shows
where the cliff is.
"""

import argparse
import hashlib
import io
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tapefec as T  # noqa: E402


def build_stream(data, k, m, block_size):
    out = io.BytesIO()
    T.encode_stream(io.BytesIO(data), out, k=k, m=m, block_size=block_size)
    return out.getvalue()


def frame_table(enc):
    frames, _info, _c, _s, _d = T.inspect_stream(io.BytesIO(enc))
    table, p = [], 0
    for fr in frames:
        n = 40 + fr["payload_len"]
        table.append((p, p + n, fr))
        p += n
    return table


def apply_damage(enc, table, rng, rate):
    """Return (damaged_bytes, n_deleted, n_corrupted)."""
    buf = bytearray(enc)
    deleted = corrupted = 0
    # decide every frame up front, then work from the back so the offsets of
    # the frames still to come are never invalidated by an earlier splice
    hits = sorted((i for i in range(len(table)) if rng.random() < rate),
                  reverse=True)
    for i in hits:
        if rng.random() < 0.5:
            s, e, _fr = table[i]
            del buf[s:e]
            deleted += 1
        else:
            s, e, fr = table[i]
            if fr["payload_len"] == 0:
                continue
            for _ in range(rng.randint(1, 8)):
                off = s + 40 + rng.randrange(fr["payload_len"])
                buf[off] ^= rng.randrange(1, 256)
            corrupted += 1
    return bytes(buf), deleted, corrupted


def trial(enc, table, data, rng, rate):
    broken, deleted, corrupted = apply_damage(enc, table, rng, rate)
    out = io.BytesIO()
    try:
        rep = T.decode_stream(io.BytesIO(broken), out)
    except T.TapefecError as exc:
        return dict(ok=False, verified=False, sha_ok=False, repaired=0,
                    unrecoverable=-1, out=b"", deleted=deleted,
                    corrupted=corrupted, error=str(exc))
    got = out.getvalue()
    return dict(ok=rep.ok,
                verified=rep.verified,
                sha_ok=(rep.sha256_match is True),
                sha_bad=(rep.sha256_match is False),
                repaired=rep.blocks_repaired,
                unrecoverable=rep.blocks_unrecoverable,
                len_out=len(got),
                exact=(got == data),
                deleted=deleted,
                corrupted=corrupted,
                error=None)


def run(data, enc, table, rate, trials, rng, label):
    stats = dict(trials=trials, exact=0, repaired=0, unrecoverable=0,
                 unverified=0, format_error=0, verified_but_wrong=0,
                 wrong_length=0, blocks_repaired=0, blocks_lost=0,
                 frames_deleted=0, frames_corrupted=0, trials_with_repair=0)
    t0 = time.time()
    for _ in range(trials):
        r = trial(enc, table, data, rng, rate)
        stats["frames_deleted"] += r["deleted"]
        stats["frames_corrupted"] += r["corrupted"]
        if r["error"]:
            # the stream-header frame itself is gone or damaged: without
            # k/m/block_size there is nothing to decode against
            stats["format_error"] += 1
            continue
        stats["blocks_repaired"] += r["repaired"]
        stats["blocks_lost"] += max(0, r["unrecoverable"])
        if r["len_out"] != len(data):
            stats["wrong_length"] += 1
        if r["unrecoverable"] > 0:
            stats["unrecoverable"] += 1
        elif not r["verified"]:
            # nothing was lost but the trailer is gone, so nothing could be
            # checked: counted separately from both success and failure
            stats["unverified"] += 1
        elif r["exact"]:
            stats["exact"] += 1
            if r["repaired"]:
                stats["repaired"] += 1
                stats["trials_with_repair"] += 1
        else:
            stats["verified_but_wrong"] += 1
    stats["seconds"] = time.time() - t0
    stats["label"] = label
    return stats


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse
                                 .RawDescriptionHelpFormatter)
    ap.add_argument("--trials", type=int, default=200,
                    help="trials per damage rate (default %(default)s)")
    ap.add_argument("--mb", type=int, default=20,
                    help="input size in MB (default %(default)s)")
    ap.add_argument("--k", type=int, default=T.DEFAULT_K)
    ap.add_argument("--m", type=int, default=T.DEFAULT_M)
    ap.add_argument("--block-size", type=int, default=T.DEFAULT_BLOCK_SIZE)
    ap.add_argument("--rates", default="0,0.01,0.05,0.15",
                    help="comma separated frame-damage rates "
                         "(default %(default)s)")
    ap.add_argument("--seed", type=int, default=20261013)
    args = ap.parse_args(argv)

    rates = [float(x) for x in args.rates.split(",")]
    rng = random.Random(args.seed)

    size = args.mb * 1000 * 1000
    print("building %d MB of random input (seed %d) ..." % (args.mb,
                                                             args.seed),
          flush=True)
    data = rng.randbytes(size)
    digest = hashlib.sha256(data).hexdigest()
    t0 = time.time()
    enc = build_stream(data, args.k, args.m, args.block_size)
    enc_time = time.time() - t0
    table = frame_table(enc)
    n_frames = len(table)
    print("encoded: %d bytes -> %d bytes in %d frames (k=%d m=%d b=%d) "
          "in %.1f s" % (len(data), len(enc), n_frames, args.k, args.m,
                         args.block_size, enc_time), flush=True)
    print("original sha256: %s\n" % digest, flush=True)

    results = []
    for rate in rates:
        label = "%.0f%%" % (rate * 100) if rate in (0.0, 0.01, 0.05, 0.15) \
            else "%.2g%%" % (rate * 100)
        print("running %d trials at %s frame damage ..." % (args.trials, label),
              flush=True)
        results.append(run(data, enc, table, rate, args.trials, rng, label))

    print()
    hdr = ("%-8s %7s %9s %13s %14s %11s %8s %10s %8s"
           % ("damage", "trials", "exact", "repaired-ok", "unrecoverable",
              "unverified", "fmt-err", "wrong-len", "secs"))
    print(hdr)
    print("-" * len(hdr))
    for s in results:
        print("%-8s %7d %9d %13d %14d %11d %8d %10d %8.1f"
              % (s["label"], s["trials"], s["exact"], s["repaired"],
                 s["unrecoverable"], s["unverified"], s["format_error"],
                 s["wrong_length"], s["seconds"]))
    print()
    print("  exact = every byte correct and the trailer's SHA-256 matched;")
    print("  repaired-ok = exact as well, but at least one block was rebuilt "
          "from parity;")
    print("  unrecoverable = at least one group exceeded its m-block repair "
          "budget;")
    print("  unverified = no data lost, but the trailer frame was damaged so "
          "nothing could be checked;")
    print("  fmt-err = the stream-header frame was damaged, so there was no "
          "k/m/block_size to decode against.")
    print()
    for s in results:
        print("  %-6s frames deleted %7d, frames corrupted %7d, "
              "blocks repaired %8d, blocks lost %7d"
              % (s["label"], s["frames_deleted"], s["frames_corrupted"],
                 s["blocks_repaired"], s["blocks_lost"]))

    total = sum(s["trials"] for s in results)
    clean = all(s["verified_but_wrong"] == 0 for s in results)
    print()
    print("CONTRACT CHECK over %d trials: whenever the decoder reported "
          "verified, the output was byte-identical to the original: %s"
          % (total, "YES" if clean else "NO"))
    print("CONTRACT CHECK over %d trials: decoded length always equal to the "
          "original length: %s (only possible to fail when the trailer frame "
          "itself was damaged)"
          % (total, "YES" if all(s["wrong_length"] == 0 for s in results)
             else "NO"))
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())