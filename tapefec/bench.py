#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Throughput benchmark for tapefec, per Reed-Solomon engine.

    nice -n 19 python3 bench.py --mb 100
    python3 bench.py --mb 20 --engines pure        # numpy not needed

For each engine (pure Python, and numpy when importable) it reports:

  * end-to-end throughput through the CLI (stdin -> stdout, as a shell
    pipeline would drive it), for encode, clean decode, and the streaming
    recovery pipeline `encode | damage --per-group m | decode`;
  * in-process CPU throughput of encode, clean decode and decode of a stream
    that lost m frames in every group (the only case that runs RS repair);
  * the isolated cost of the parity step, rs_encode_group().

plus the engine-independent floor: SHA-256 and CRC-32 over the stream.

Wall-clock time is noisy on shared machines, so each CLI measurement is
repeated and the run with the best wall time is reported together with the
child processes' user+sys CPU time.  Throughput is quoted per input byte.
"""

import argparse
import hashlib
import io
import os
import random
import resource
import subprocess
import sys
import tempfile
import time
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tapefec as T  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tapefec.py")


def child_cpu():
    ru = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime


def sh(cmd, stdin, stdout, env, reps=3):
    """Run a shell command line; return (best wall, cpu of that run)."""
    runs = []
    for _ in range(reps):
        c0 = child_cpu()
        t0 = time.perf_counter()
        with open(stdin, "rb") as fin, open(stdout, "wb") as fout:
            p = subprocess.run(cmd, shell=True, stdin=fin, stdout=fout,
                               stderr=subprocess.DEVNULL, env=env)
        dt = time.perf_counter() - t0
        cpu = child_cpu() - c0
        if p.returncode != 0:
            raise SystemExit("command failed (%d): %s" % (p.returncode, cmd))
        runs.append((dt, cpu))
    return min(runs, key=lambda r: r[0])


class Discard:
    """Output sink for in-process runs, so the numbers measure the codec
    rather than a growing BytesIO."""

    def write(self, data):
        return len(data)


def cpu_time(fn, reps=3):
    best = None
    for _ in range(reps):
        t0 = time.process_time()
        fn()
        dt = time.process_time() - t0
        best = dt if best is None else min(best, dt)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mb", type=int, default=100)
    ap.add_argument("--k", type=int, default=T.DEFAULT_K)
    ap.add_argument("--m", type=int, default=T.DEFAULT_M)
    ap.add_argument("--block-size", type=int, default=T.DEFAULT_BLOCK_SIZE)
    ap.add_argument("--engines", default=None,
                    help="comma-separated engines to measure "
                         "(default: pure, plus numpy if importable)")
    ap.add_argument("--scratch", default=None)
    args = ap.parse_args()

    engines = (args.engines.split(",") if args.engines else
               ["pure"] + (["numpy"] if T.numpy_available() else []))
    for e in engines:
        T.resolve_engine(e)            # fail early on a bad name

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
    k, m, b = args.k, args.m, args.block_size

    print("writing %d MB of random input ..." % args.mb, flush=True)
    raw = random.Random(20261013).randbytes(n)
    with open(plain, "wb") as fh:
        fh.write(raw)

    enc_buf = io.BytesIO()
    T.encode_stream(io.BytesIO(raw), enc_buf, k=k, m=m, block_size=b)
    enc_bytes = enc_buf.getvalue()
    broken_buf = io.BytesIO()
    T.damage_stream(io.BytesIO(enc_bytes), broken_buf, per_group=m, seed=1)
    broken = broken_buf.getvalue()

    py = sys.executable
    fec_cmd = "%s %s" % (py, SCRIPT)
    enc_cmd = "%s encode -k %d -m %d --block-size %d" % (fec_cmd, k, m, b)
    dec_cmd = "%s decode -q" % fec_cmd
    pipe_cmd = "%s | %s damage --per-group %d --seed 1 | %s" % (
        enc_cmd, fec_cmd, m, dec_cmd)

    results = {}
    for engine in engines:
        env = dict(os.environ, TAPEFEC_ENGINE=engine)
        r = results[engine] = {}
        print("[%s] CLI encode ..." % engine, flush=True)
        r["cli_enc"] = sh(enc_cmd, plain, fec, env)
        print("[%s] CLI decode ..." % engine, flush=True)
        r["cli_dec"] = sh(dec_cmd, fec, back, env)
        assert os.path.getsize(back) == n, "CLI round trip changed the size"
        print("[%s] CLI encode | damage | decode ..." % engine, flush=True)
        r["cli_pipe"] = sh(pipe_cmd, plain, back, env)
        with open(back, "rb") as fh:
            assert fh.read() == raw, "recovery pipeline did not restore input"

        print("[%s] in-process ..." % engine, flush=True)
        r["enc"] = cpu_time(lambda: T.encode_stream(
            io.BytesIO(raw), Discard(), k=k, m=m, block_size=b,
            engine=engine))
        r["dec"] = cpu_time(lambda: T.decode_stream(
            io.BytesIO(enc_bytes), Discard(), engine=engine))

        def repair():
            rep = T.decode_stream(io.BytesIO(broken), Discard(),
                                  engine=engine)
            assert rep.verified and rep.blocks_repaired
        r["repair"] = cpu_time(repair)

        g = T.generator_matrix(k, m)
        blocks = [raw[i * b:(i + 1) * b] for i in range(k)]
        encode_group = T.resolve_engine(engine)[1]
        reps = 64
        r["rs"] = cpu_time(lambda: [encode_group(blocks, b, g, m)
                                    for _ in range(reps)]) / reps * (n / (k * b))

    sha = cpu_time(lambda: hashlib.sha256(raw).digest())
    crc = cpu_time(lambda: zlib.crc32(enc_bytes)) * n / len(enc_bytes)

    def mbs(secs):
        return n / secs / 1e6

    print()
    print("tapefec %s  k=%d m=%d block_size=%d  input %d MB  single core"
          % (T.__version__, k, m, b, args.mb))
    print("host: %s %s, Python %s%s"
          % (os.uname().sysname, os.uname().release,
             sys.version.split()[0],
             ", numpy " + __import__("numpy").__version__
             if T.numpy_available() else ""))
    print()
    head = "%-44s" % "MB/s of input" + "".join("%12s" % e for e in engines)
    print(head)
    print("-" * len(head))
    rows = [
        ("encode         CLI, wall", lambda r: mbs(r["cli_enc"][0])),
        ("encode         CLI, child CPU", lambda r: mbs(r["cli_enc"][1])),
        ("decode         CLI, wall", lambda r: mbs(r["cli_dec"][0])),
        ("decode         CLI, child CPU", lambda r: mbs(r["cli_dec"][1])),
        ("enc|damage|dec CLI pipeline, wall", lambda r: mbs(r["cli_pipe"][0])),
        ("encode         in-process CPU", lambda r: mbs(r["enc"])),
        ("decode clean   in-process CPU", lambda r: mbs(r["dec"])),
        ("decode m lost/group, in-process CPU", lambda r: mbs(r["repair"])),
        ("  rs_encode_group() alone", lambda r: mbs(r["rs"])),
    ]
    for name, fn in rows:
        print("%-44s" % name + "".join("%12.0f" % fn(results[e])
                                       for e in engines))
    print("%-44s%12.0f   (any engine)" % ("  hashlib.sha256 over the input",
                                          mbs(sha)))
    print("%-44s%12.0f   (any engine)" % ("  zlib.crc32 over the encoded stream",
                                          mbs(crc)))
    print()
    print("encoded size %d bytes (%.3f%% overhead); the pipeline row "
          "repairs %d lost frames per group" % (len(enc_bytes),
                                                (len(enc_bytes) / n - 1) * 100,
                                                m))
    for f in (plain, fec, back):
        os.unlink(f)
    print("(scratch files removed)")


if __name__ == "__main__":
    main()
