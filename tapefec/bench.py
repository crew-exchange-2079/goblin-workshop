#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Throughput benchmark for tapefec.

    nice -n 19 python3 bench.py --mb 100

Reports end-to-end throughput through the CLI (stdin -> stdout, as a shell
pipeline would drive it) and the isolated cost of the Reed-Solomon parity step.

Wall-clock time on this workstation is noisy -- identical runs have been
observed at 0.09 s and 1.51 s for the same 24 MB of work -- so each measurement
is repeated and the **best** wall time is reported, and the child process's
user+sys CPU time (which is what the algorithm actually costs) is reported next
to it.  Throughput is quoted from CPU time.
"""

import argparse
import io
import tempfile
import os
import random
import resource
import subprocess
import sys
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tapefec as T  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tapefec.py")


def child_cpu():
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def sh(cmd, stdin=None, stdout=None):
    """Run the CLI the way a shell pipeline would; return (best wall, matching cpu)."""
    runs = []
    for _ in range(3):
        c0 = child_cpu()
        t0 = time.time()
        with open(stdin, "rb") as fin, open(stdout, "wb") as fout:
            p = subprocess.run(cmd, stdin=fin, stdout=fout,
                               stderr=subprocess.DEVNULL)
        dt = time.time() - t0
        cpu = child_cpu() - c0
        if p.returncode != 0:
            raise SystemExit("command failed (%d): %s"
                             % (p.returncode, " ".join(cmd)))
        runs.append((dt, cpu))
    best_wall, best_cpu = min(runs, key=lambda r: r[0])
    return best_wall, best_cpu


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=100)
    ap.add_argument("--k", type=int, default=T.DEFAULT_K)
    ap.add_argument("--m", type=int, default=T.DEFAULT_M)
    ap.add_argument("--block-size", type=int, default=T.DEFAULT_BLOCK_SIZE)
    ap.add_argument("--scratch", default=None)
    args = ap.parse_args()

    if args.scratch is None:
        _td = tempfile.TemporaryDirectory()
        scratch = _td.name
    else:
        os.makedirs(args.scratch, exist_ok=True)
        scratch = args.scratch
        _td = None
    plain = os.path.join(scratch, "bench.plain")
    fec = os.path.join(scratch, "bench.tapefec")
    back = os.path.join(scratch, "bench.back")
    n = args.mb * 1000 * 1000

    if not os.path.exists(plain) or os.path.getsize(plain) != n:
        print("writing %d MB of random input ..." % args.mb, flush=True)
        with open(plain, "wb") as fh:
            fh.write(random.Random(20261013).randbytes(n))
    data = os.path.getsize(plain)

    enc_cmd = [sys.executable, SCRIPT, "encode", "-k", str(args.k), "-m",
               str(args.m), "--block-size", str(args.block_size)]
    dec_cmd = [sys.executable, SCRIPT, "decode", "-q"]

    print("measuring encode (CLI, stdin -> stdout) ...", flush=True)
    enc_wall, enc_cpu = sh(enc_cmd, stdin=plain, stdout=fec)
    enc_size = os.path.getsize(fec)
    print("measuring decode (CLI, stdin -> stdout) ...", flush=True)
    dec_wall, dec_cpu = sh(dec_cmd, stdin=fec, stdout=back)

    raw = open(plain, "rb").read()
    buf = io.BytesIO()
    t0 = time.process_time()
    T.encode_stream(io.BytesIO(raw), buf, k=args.k, m=args.m,
                    block_size=args.block_size)
    cpu_enc = data / (time.process_time() - t0) / 1e6
    enc_bytes = buf.getvalue()

    out = io.BytesIO()
    t0 = time.process_time()
    T.decode_stream(io.BytesIO(enc_bytes), out)
    cpu_dec = data / (time.process_time() - t0) / 1e6

    b, k, m = args.block_size, args.k, args.m
    g = T.generator_matrix(k, m)
    blocks = [bytes(b) for _ in range(k)]
    reps = 64
    t0 = time.process_time()
    for _ in range(reps):
        T.rs_encode_group(blocks, b, g, m)
    rs_mbs = reps * k * b / (time.process_time() - t0) / 1e6

    t0 = time.process_time()
    c = 0
    for i in range(0, len(enc_bytes), 1 << 16):
        c = zlib.crc32(enc_bytes[i:i + (1 << 16)], c)
    crc_mbs = len(enc_bytes) / (time.process_time() - t0) / 1e6

    rows = [
        ("encode  CLI (stdin->stdout), wall", enc_wall, data / enc_wall / 1e6),
        ("encode  CLI, child CPU", enc_cpu, data / enc_cpu / 1e6),
        ("decode  CLI (stdin->stdout), wall", dec_wall, data / dec_wall / 1e6),
        ("decode  CLI, child CPU", dec_cpu, data / dec_cpu / 1e6),
        ("encode  in-process CPU", None, cpu_enc),
        ("decode  in-process CPU", None, cpu_dec),
        ("  rs_encode_group() alone", None, rs_mbs),
        ("  zlib.crc32 over the whole encoded stream", None, crc_mbs),
    ]
    print()
    print("tapefec %s  k=%d m=%d block_size=%d  input %d MB  single core"
          % (T.__version__, k, m, b, args.mb))
    print("host: %s %s" % (os.uname().sysname, os.uname().release))
    print()
    print("%-46s %9s %11s" % ("stage", "seconds", "MB/s"))
    print("-" * 68)
    for name, secs, mbs in rows:
        print("%-46s %9s %11.1f"
              % (name, "-" if secs is None else "%.2f" % secs, mbs))
    print()
    print("encoded size %d bytes (%.3f%% overhead)"
          % (enc_size, (enc_size / data - 1) * 100))
    assert os.path.getsize(back) == data, "round trip changed the size"
    print("round trip through the CLI preserved all %d bytes" % data)
    for f in (plain, fec, back):
        os.unlink(f)
    print("(scratch files removed)")


if __name__ == "__main__":
    main()