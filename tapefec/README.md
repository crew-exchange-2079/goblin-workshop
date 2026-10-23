# tapefec — streaming forward-error-correction for tar-to-tape

**A pure-Python, standard-library-only educational prototype, with an optional
numpy fast path that writes byte-identical output. All testing is against
synthetic data; nothing here has been run against a physical tape, drive,
library or any vendor's hardware or software, and no compatibility is claimed.**

`torture-output.txt` records 800 trials of random synthetic frame damage in which
the decoder never reported "verified" for a wrong output. See `DESIGN.md` §2 for
how the Reed–Solomon code is built and `test_tapefec.py` for the current 105 unit
tests (the fixes behind them were also confirmed by 8 separately verified
regression reproductions). The torture table below is a historical run from
before those fixes; the throughput table was re-measured for 1.1.0.

**Reproduce (stdlib only, nothing installed):**
`python3 -m unittest test_tapefec && python3 torture.py && sh demo.sh`
(with numpy installed the same suite also checks the fast path against the
pure one; without it those tests skip).

Historical original 68-test run (the current suite includes later regressions):

```console
$ nice -n 19 python3 -m unittest test_tapefec
....................................................................
----------------------------------------------------------------------
Ran 68 tests in 2.2s

OK
```

---

## What it is

The default v2 Cauchy format is described here; legacy v1 compatibility has
weaker recovery guarantees as explained under Format versions and DESIGN §2.2.

`tapefec encode` takes any byte stream, chops it into 64 KiB blocks, groups 20 of
them, and appends 4 Reed–Solomon parity blocks per group before writing frames to
stdout. `tapefec decode` reads that back, repairs up to 4 damaged or missing
frames in every group of 24, and writes the original bytes to stdout.

It is a `tar | zstd | age` kind of tool: it is not a file format manager, it does
not seek, it does not know what a tape is. It just puts 20 % redundancy and
per-frame CRCs into a pipe so that a bad block on a cartridge is a repair instead
of a lost archive.

Python 3 standard library only. No dependencies, nothing to install, nothing to
compile. If numpy happens to be importable, the parity arithmetic uses it and
encodes about 2.5× faster; the bytes on the wire are identical either way
(see [Engines](#engines-pure-python-and-numpy)).

## Install

There is nothing to install. `chmod +x tapefec.py` and put a symlink named
`tapefec` on your `PATH` if you want the short name:

```console
$ chmod +x tapefec.py && ln -s "$PWD/tapefec.py" ~/.local/bin/tapefec
```

Without the symlink, write `./tapefec.py` or `python3 tapefec.py`. The examples
below use `tapefec`; substitute as needed.

## Usage

### The basic pipeline — write to a FILE

`tapefec` cannot re-read what it has written, so **always write to a regular
file**, never to `/dev/nst0` or any other device. If the pipe to a drive breaks
mid-archive, the parity for the frames you already wrote is still on the tape —
but you can only use it if the frames are actually there and readable.

```console
$ tar -c -C /srv photos docs | tapefec encode > photos.tapefec
$ ls -l photos.tapefec
-rw-r--r-- 1 user staff 1573960 Aug 13 23:43 photos.tapefec     # for 1310720 bytes in
$ tapefec decode < photos.tapefec | tar -tvf -
```

Note the 20.08 % overhead: 1 310 720 bytes in, 1 573 960 bytes on the tape.

Recommended for a real archive: compress, encrypt, then FEC, then put the result
on the media.

```console
$ tar -c -C /srv photos \
    | zstd -19 --long=27 \
    | age -r age1ql3z7hjy54pw3hyww5ayyfg7zqgvc7w3j2elw8zmrj2kg5sfn9aqmcac8p \
    | tapefec encode -k 20 -m 4 --block-size 65536 \
    | dd of=/media/archive-2026-10-13.tapefec bs=1M conv=fsync status=progress
```

(That age recipient is a made-up example key; replace it with your own.)

Recover, in the mirror image:

```console
set -o pipefail
$ tapefec decode < /media/archive-2026-10-13.tapefec \
    | age -d -i ~/.age/key.txt \
    | zstd -d \
    | tar -xv -C /restore/photos
$ echo "exit status: ${PIPESTATUS[0]}"     # tapefec's, not tar's: 0 = every byte recovered and verified
```

`tapefec decode` writes a JSON report to stderr and exits non-zero if anything was
unrecoverable or if the trailer's SHA-256 did not match:

```console
$ tapefec decode < photos.tapefec > photos.tar 2> report.json
$ echo $?
1
$ python3 -c "import json;d=json.load(open('report.json'));print(d['unrecoverable_ranges'])"
[{'group': 7, 'block': 3, 'start': 9175040, 'end': 9240576}]
```

Unrecoverable ranges are written as zeros so that `tar` sees the right stream
length and fails at the right place instead of silently losing alignment. Do not
trust a restore that exited non-zero — re-read the tape and try again, because
the same read often succeeds on a second pass.

### Watching it repair something

Four data frames deleted out of one group of 24 — exactly the `m` the format
promises. The output is byte-identical to the input and the exit status is 0:

```console
$ tapefec decode --text < photos.tapefec > photos.tar
tapefec: 1 group(s); 16 blocks ok, 4 repaired, 0 unrecoverable; read 22 frames, discarded 0 garbage byte(s), 0 resync(s), 0 bad payload(s)
tapefec: group 0: repaired (repaired=4 lost_data=4 lost_parity=0)
{"ok": true, "verified": true, "blocks_repaired": 4, "blocks_unrecoverable": 0, "trailer_seen": true, "sha256_match": true}
$ echo $?
0
```

The last stderr line is always a JSON object — per-group status, byte ranges,
digest result, and the reader's own counters — so a script does not have to
parse the human lines. `--text` adds the human lines; `--quiet` suppresses the
JSON on a clean run. With `--quiet --strict`, a fully verified repair also
suppresses JSON while returning status 1. Unrecoverable data, a failed digest,
or a missing trailer still emits the JSON report, even if other blocks repaired.

### Choosing `-k`, `-m` and `--block-size`

```console
$ tapefec encode --help
$ tapefec encode -k 20 -m 4 --block-size 65536 < archive.tar > archive.tapefec
```

| knob | default | meaning |
|---|---|---|
| `-k` | 20 | data blocks per parity group |
| `-m` | 4 | parity blocks per group — **this is how many blocks in a group can be lost** |
| `--block-size` | 65536 | data block size in bytes |

`decode` needs none of these: the stream header frame carries them.

| k | m | block | overhead | repairable per group |
|---|---|---|---|---|
| 20 | 2 | 64 KiB | 10.07 % | any 2 of 22 |
| 20 | 4 | 64 KiB | 20.07 % | any 4 of 24 (**default**) |
| 32 | 4 | 64 KiB | 12.54 % | any 4 of 36 |
| 20 | 4 | 1 MiB | 20.006 % | any 4 of 24 |

Overhead is `m/k` plus `40·(k+m)/(k·b)`; see `DESIGN.md` §1.5.

### Streaming: every stage is stdin → stdout

`encode`, `decode` and `damage` are filters. Each holds at most a couple of
parity groups in memory and writes a group as soon as it is finished, so they
compose in a pipeline with nothing spilled to disk in between, and the output
of a long `tar` starts arriving long before `tar` exits:

- `encode` writes a group's data frames as soon as the group is full, then its
  parity, and flushes after every group.
- `decode` reads whatever the pipe has (it does not wait for a megabyte), emits
  a group as soon as all of its frames are in (or the next group starts), and
  holds back only `k+1` blocks so the trailer can trim the final one.
- On Linux the CLI grows its pipes from 64 KiB to 1 MiB (best effort,
  `F_SETPIPE_SZ`) so neighbouring stages overlap instead of taking turns.

### Recovery demo: `tapefec damage`

`tapefec damage` is a stand-in for a bad tape: a filter that drops or corrupts
data/parity frames on the way through, deterministically for a given `--seed`.
The stream header and trailer pass untouched, so whether the decoder can repair
the result is down to the parity alone.

```console
$ tapefec encode < archive.tar | tapefec damage --per-group 4 | tapefec decode > restored.tar
$ tapefec damage --help        # --per-group N | --rate P, --mode drop|corrupt|mixed, --seed S
```

`demo.sh` runs it end to end on 20 MB of random input — once with exactly `m`
frames destroyed in every group of 24, once with `m+1`. This is its unedited
output (`sh demo.sh`, numpy engine):

```console
$ sh demo.sh
== 20 MB in; 4 of every 24 frames destroyed in transit (k=20 m=4)
tapefec: encoded 20000000 bytes into 306 data + 64 parity blocks (k=20 m=4 block=65536 stream_id=0, 372 frames, engine=numpy)
tapefec: damage: {"corrupted": 38, "dropped": 26, "frames": 372, "groups": 16}
tapefec: 16 group(s); 252 blocks ok, 54 repaired, 0 unrecoverable; read 346 frames, discarded 0 garbage byte(s), 0 resync(s), 38 bad payload(s)
tapefec: group 0: repaired (repaired=4 lost_data=4 lost_parity=0)
tapefec: group 1: repaired (repaired=3 lost_data=3 lost_parity=1)
tapefec: group 2: repaired (repaired=2 lost_data=2 lost_parity=2)
tapefec: group 3: repaired (repaired=4 lost_data=4 lost_parity=0)
-> exit status 0, output is byte-identical to the input

== same again with 5 of every 24 destroyed: one more than m
tapefec: encoded 20000000 bytes into 306 data + 64 parity blocks (k=20 m=4 block=65536 stream_id=0, 372 frames, engine=numpy)
tapefec: damage: {"corrupted": 38, "dropped": 42, "frames": 372, "groups": 16}
tapefec: 16 group(s); 0 blocks ok, 0 repaired, 69 unrecoverable; read 330 frames, discarded 0 garbage byte(s), 0 resync(s), 38 bad payload(s)
tapefec: group 0: unrecoverable (repaired=0 lost_data=5 lost_parity=0)
tapefec: group 1: unrecoverable (repaired=0 lost_data=3 lost_parity=2)
-> 69 unrecoverable range(s), first {'block': 2, 'end': 196608, 'group': 0, 'start': 131072}; output still 20000000 bytes long
-> exit status 1
```

The second run is the honest failure mode: every group is one frame past its
budget, the decoder says exactly which byte ranges are zeros, keeps the output
at the original length, and exits 1. (`blocks ok` counts blocks in groups that
decoded; an unrecoverable group's surviving blocks are still written, but are
not counted there.)

### Looking at a stream without decoding it

```console
$ tapefec inspect < photos.tapefec | head -8
stream: k=20 m=4 block_size=65536 flags=0 total_length=1310720
sha256(original): 6373d38a49171ddb24e9719db643f3eb549ce9e59ce7aa267156a7c1aeb4dfce
frames: 26 (header=1, data=20, parity=4, trailer=1)  damaged payloads: 0
read stats: {"bad_payload": 0, "frames": 26, "garbage_bytes": 0, "resyncs": 0, "truncated": 0}

frame    type     stream block_id     payload      ok
0        header   0      0            16           True
1        data     0      0            65536        True
2        data     0      1            65536        True
```

`tapefec inspect --json` gives the same map as JSON, which is what you want if
you are scripting a verification pass over a whole tape library.

`inspect` keeps one small record per frame (it is the only path that holds
the whole frame list); `encode`, `decode` and `damage` are the bounded-memory
paths.

### Engines: pure Python and numpy

```console
$ tapefec --engine pure encode < in > out      # or TAPEFEC_ENGINE=pure
$ tapefec --engine numpy encode < in > out     # exit 2 if numpy is missing
```

| engine | what it does |
|---|---|
| `pure` | the reference: `bytes.translate` per (parity row, block) plus big-integer XOR (DESIGN §2.4) |
| `numpy` | one `np.take` per data block against a 256-entry table that packs that block's products for up to 8 parity rows into one machine word, then a lane-wise XOR (DESIGN §2.4) |
| `auto` (default) | `numpy` if it imports, else `pure`. A decode only imports numpy once a group actually needs repair, so clean decodes, `inspect` and `damage` never pay for it |

Only the Reed–Solomon arithmetic differs; framing, CRC, SHA-256 and every
decision the decoder makes are shared code. The output is the same bytes, and
the decode report the same JSON, whichever engine runs. `test_tapefec.py` pins
that three ways: **golden SHA-256 digests** of encoded streams (captured from
the 1.0.0 pure encoder, before the fast path existed; they cover v1 and v2, a
short final group, `k=1`, `k=255`, and `m=9`/`m=17` so the 8-row lane split is
exercised) that both engines must reproduce; randomized `encode_group` and
`repair` comparisons including singular v1 patterns and short blocks; and
damaged-stream decodes whose output and full report must match. The `encode`
summary line names the engine that ran.

### Exit status

| code | meaning |
|---|---|
| 0 | every byte recovered **and** the trailer's SHA-256 matched |
| 1 | something was unrecoverable, the digest disagreed, or the trailer is gone |
| 2 | not a tapefec stream, or bad arguments |

`--strict` additionally fails when any block had to be rebuilt from parity —
useful in a verification cron job, where "silently repaired" should be an alert.

## Why not par2 / redupe

**par2** (`par2cmdline`) is the usual answer to "how do I protect an archive", and
it is a good answer — for a *file*. It needs random access: it reads the input
twice, once to hash it and once to build a recovery set. A pipe cannot be rewound,
and `tar | par2` means `tar` has to spill the entire archive onto disk first,
which is the step you were trying to get away from. It also has no notion of
frames, so it cannot resynchronise: one bad sector in the middle of a 2 TB tape
takes the whole recovery set with it unless you had the good read to hand.
A commenter on the [Hacker News thread](https://news.ycombinator.com/item?id=30099540)
that motivated this tool reached for exactly par2; other comments there argue that
application-level interleaving is the part that matters and that drive-internal
ECC does not cover you.

**redupe** was also mentioned in that discussion. It has not been evaluated here,
and this document makes no claim about its current availability.

**infectious** (Go) and **blockyarchive** (block-level FEC container) were also
suggested on that thread. `tapefec` is not competing with them: it is the smallest
thing that does *streaming, framed, interleaved, per-block CRC* FEC in a
dependency-free script you can read in an afternoon — which is also the property
that matters when the archive you are trying to rescue is 15 years old and the
toolchain that reads it is the one you have left.

## Damage tolerance — the torture table

200 trials at each of four frame-damage rates over one 20 MB random stream,
k=20, m=4, 64 KiB blocks (800 trials total). Full output in
`torture-output.txt`; reproduce with `nice -n 19 python3 torture.py`.

```
damage    trials     exact   repaired-ok  unrecoverable  unverified  fmt-err  wrong-len     secs
------------------------------------------------------------------------------------------------
0%           200       200             0              0           0        0          0      8.0
1%           200       192           187              0           6        2          3      8.7
5%           200       159           159             12          17       12          9     12.9
15%          200         0             0            166           1       33         25     14.0

  0%     frames deleted       0, frames corrupted       0, blocks repaired        0, blocks lost       0
  1%     frames deleted     366, frames corrupted     382, blocks repaired      594, blocks lost       0
  5%     frames deleted    1807, frames corrupted    1943, blocks repaired     2784, blocks lost      52
  15%    frames deleted    5611, frames corrupted    5544, blocks repaired     4277, blocks lost    3443

CONTRACT CHECK over 800 trials: whenever the decoder reported verified, the output
was byte-identical to the original: YES
```

The damage unit is the **frame**, not the byte: each frame independently suffers
damage with probability `rate`, half deleted outright and half corrupted in the
payload. That is how a drive reports a bad block.

| column | meaning |
|---|---|
| exact | every byte correct and the trailer's SHA-256 matched |
| repaired-ok | also exact, but at least one block was rebuilt from parity |
| unrecoverable | some group exceeded its `m`-block repair budget |
| unverified | nothing lost, but the trailer frame was damaged so nothing could be checked |
| fmt-err | the stream-header frame was damaged — no `k`/`m`/`block_size` to decode against |
| wrong-len | output length differed (only possible when the trailer was lost) |

The cliff is predictable: `P(group survives) = P(Binomial(24, p) ≤ 4)`, so a
20 MB stream survives with probability `1.0 / 0.9999 / 0.909 / 0.005` at
0 % / 1 % / 5 % / 15 %. The observed exact column tracks it, with the gap at 1 %
and 5 % explained by trials whose *header or trailer frame* was itself hit.
**1 % is comfortable, 5 % usually works, 15 % is past the cliff.**

## Throughput

Measured for 1.1.0 with `nice -n 19 python3 bench.py --mb 100` on a 4-vCPU
cloud VM (Intel Xeon @ 2.80 GHz, AVX-512, **no SHA-NI**), Linux 6.18, Python
3.13.17, numpy 2.5.3, k=20 m=4 64 KiB blocks. Two consecutive runs agreed to
within ~10 %; the table shows the first. MB/s are per byte of *input*. Not
representative of other machines; rerun `bench.py` on yours.

| stage | pure | numpy |
|---|---:|---:|
| encode, CLI stdin→stdout, wall | 70 | **122** |
| decode (clean), CLI, wall | **188** | 142¹ |
| `encode \| damage --per-group 4 \| decode`, wall | 46 | **93** |
| encode, in-process CPU | 83 | **225** |
| decode clean, in-process CPU | 247 | 256 |
| decode with 4 lost frames/group, in-process CPU | 93 | **194** |
| `rs_encode_group()` alone | 142 | **730** |
| SHA-256 over the input (either engine) | 392 | 392 |
| CRC-32 over the encoded stream (either engine) | 3 179 | 3 179 |

¹ `bench.py` forces the engine, so this row pays numpy's import (~75 ms wall)
for a decode that does no RS work. Under the default `auto` a clean decode
never imports numpy and runs at the pure figure.

For comparison on the same VM, 1.0.0 managed 80 MB/s for `encode` and
54 MB/s for `encode | decode` (best of three); 1.1.0 does 148 and 123 under
`auto`, and 71 for `encode | decode` with `--engine pure` — that last gain is
the streaming changes alone.

What the profile says (`python3 -m cProfile`, 50 MB):

- **Pure encode** spent 37 % of its time in `int.from_bytes` and 21 % in
  `bytes.translate`, i.e. most of it in the big-integer XOR trick, and 20 % in
  SHA-256. The numpy kernel replaces both halves of the trick; what remains is
  mostly SHA-256, which is the same C code for both engines.
- **Clean decode is SHA-256-bound** (62 % of its time on this host, which has
  no SHA-NI), because a clean stream needs no RS arithmetic at all. No engine
  can help there; a CPU with SHA extensions will.
- **Repair** runs the same kernel twice per damaged group (syndromes, then the
  `e×e` inverse), so it tracks the encode speed-up.
- **Pipelines** were slower than their slowest stage in 1.0.0: the decoder
  read in blocking 1 MiB chunks and could only finish a group when the next
  one began, while the encoder computed parity before writing any of a
  group's frames, so the two ends took turns. Fixing that (see
  [Streaming](#streaming-every-stage-is-stdin--stdout)) is what lifts the
  pipeline row.
- numpy's import is ~75 ms wall; with its BLAS thread pool it was ~0.26 s CPU,
  so the CLI defaults `OPENBLAS_NUM_THREADS`/`OMP_NUM_THREADS`/
  `MKL_NUM_THREADS` to 1 (tapefec makes no BLAS calls; values you export win).
  Even so, numpy only pays for itself on encode above roughly 10 MB of input
  per process: at `--mb 10` both engines encode at ~51 MB/s through the CLI.

Reading that honestly:

- **Still single-threaded.** Each stage uses one core; a pipeline spreads three
  stages over three cores and is bounded by the slowest.
- **Pure Python may be adequate for a single stream.** Native LTO drive speeds are
  published by the vendors and are of the same order as the encode figures above, but
  no tape drive was used here, so no claim is made about keeping a drive streaming.
  It would not keep up with an NVMe-to-RAID array doing multi-stream writes, and it
  is not trying to.

**What a C or Rust port would cost.** The format is not the problem; the
remaining per-byte work is. In C, the parity step is a table-driven
multiply-accumulate: `k*m = 80` GF(2⁸) multiplies per 64 KiB byte position,
~5.2 M byte-ops per 1.25 MiB group. With a 64 KiB lookup table and SIMD
(`_mm_shuffle_epi8` in C, `u8x16_shuffle` in Rust) this is a handful of
`pshufb`+`pxor` per 16 bytes and should run at **several GB/s** — CRC32 and
SHA-256 have hardware instructions on most current x86-64 and ARMv8 CPUs
(`crc32` + `sha256rni`), which alone put a floor of ~10 GB/s on the framing.
So: a good C implementation should land at **1–3 GB/s** encode and **5–10 GB/s**
decode, versus the low hundreds of MB/s here. These are estimates, not
measurements.

The honest engineering conclusion is that this should *not* be ported until
someone needs multi-stream tape writes: a Python file that anyone can read and
audit is worth a lot when the data is archival, and the numpy path keeps that
property because the pure path stays the reference it is tested against.

## Repository layout

| file | what it is |
|---|---|
| `tapefec.py` | the whole tool: framing, GF(2⁸)/Reed–Solomon (pure and numpy engines), encoder, decoder, damage filter, CLI |
| `DESIGN.md` | wire format, RS construction, overhead maths, and what the format does **not** protect against |
| `test_tapefec.py` | 105 unittest tests: GF identities, RS erasure patterns, round trips, damage, golden digests, engine parity, streaming |
| `torture.py` | 200 trials × 4 damage rates over a 20 MB stream |
| `torture-output.txt` | the torture table, pasted from a historical run |
| `bench.py` | throughput measurement, per engine |
| `demo.sh` | streaming recovery demo: `encode \| damage \| decode` with `m` and `m+1` losses per group |

## Format versions

The CLI encoder defaults to **format version 2 with a Cauchy parity matrix**.
The decoder also accepts legacy v1 Vandermonde streams. The v2 construction
recovers any `m` erased data/parity frames per group when surviving frames are
intact; legacy v1 has singular patterns for some parameters and does not share
that guarantee. V2 parity row zero is not generally a plain XOR. See DESIGN §2.2.
No wire layout or version is changed by the current reporting/validation fixes
or by the 1.1.0 engines; the golden-digest tests hold the encoder to that.

Decoder behaviour change in 1.1.0: a CRC-valid parity payload whose length is
not exactly `block_size`, or a data payload longer than it, is now an erasure.
Before, with `block_size < 40`, a 40-byte parity payload got past the reader's
cap and crashed repair with an uncaught `OverflowError`, and a short one was
"repaired" into wrong bytes.


What is included:

- `DESIGN.md`: 40-byte frame header with offset table, systematic Cauchy erasure
  code over GF(2⁸)/0x11D for default format version 2, erasure-only decoding with
  the feasibility condition `erasures + lost_parity ≤ m`, overhead maths, and an
  explicit "does NOT protect against" list.
- `tapefec.py` (~1 500 lines, one file, `--help`, `-k`, `-m`, `--block-size`,
  `--stream-id`, `--engine`). Encode, decode and damage are all streaming with
  memory bounded at about `2·(k+m)·block_size`.
- 105 unit tests: an original 68-test set plus later regression tests covering
  reporting, EOF handling and size validation, and the 1.1.0 golden-digest,
  engine-parity, streaming and damage-filter tests.
- `torture.py` (200 trials per damage rate over 20 MB; table in
  `torture-output.txt`), `bench.py` and `demo.sh`. The torture output is
  historical; the throughput table is a 1.1.0 measurement on one VM; the C/Rust
  port cost above is an estimate, not a measurement.

## Limitations

- **CRC-32C.** DESIGN §1.2 argues Castagnoli is the better tape choice; the
  format ships CRC-32/ISO-HDLC because `zlib.crc32` is C-speed and CRC-32C is not
  in the stdlib. There is no `--crc` switch. A port with SSE4.2 should use
  CRC-32C and version the format.
- **The stream header frame is unprotected.** If it is lost or corrupted the
  whole stream is undecodable (exit 2) — 56 bytes cannot be protected by the
  parity of the blocks that follow. The 4 reserved bytes at offset 32..35 plus
  `header_crc32` at 36..39 leave only 4 spare bytes in the header, so a copy
  of `k`/`m`/`block_size` would need a longer frame header. Absent in both supported versions.
- **Trailing-length ambiguity.** If the trailer frame *and* the whole final group
  are destroyed, the decoder cannot know how long the last group was and the
  output length can be wrong. The torture run hits this at 15 % damage. Reported
  in the JSON report rather than papered over.
- **No fuzzing.** Malformed `payload_len` is bounded by a cap derived from the
  header frame before allocation, but there has been no adversarial-input campaign.
  Do not point this at untrusted data yet.
- **No real tape.** Nothing here has been written to or read from an LTO drive.
  It has never seen `mt`, filemarks, EOT, a changer, or a drive that lies about
  a successful write. That is the single biggest gap between this prototype and
  something you would trust with an archive.
- **No `reduce`/`reconstruct` subcommands.** `decode` always writes the stream,
  even when a group is unrecoverable. A tool that could dump only the good
  frames, or repair in place from a second read of the same tape, would be more
  useful in practice than what is here.
- **No multi-stream / multi-file support.** One stream in, one stream out. Real
  tape libraries want `k` independent FEC streams across `k` drives so that a
  single drive failure is a per-stream event.
- **Single-threaded, and only the RS step is accelerated.** The numpy engine
  speeds up parity and repair; SHA-256, CRC-32 and I/O are unchanged, and SHA-256
  now dominates both encode and clean decode on CPUs without SHA extensions.
- **RS is erasure-only.** A frame whose payload is damaged *without* breaking its
  CRC-32 is treated as intact and will poison the repair of its group. Adding
  error correction (Berlekamp–Massey) rather than erasure decoding would catch
  some of these. Chosen because the CRC already names the damaged frames and
  erasure decoding is an `m×m` matrix solve instead of a locator polynomial.
- **The torture damage model is frame-level**, not byte-level, and 50 % of hits
  are deletions. That matches how a drive reports a bad block; it does not match
  a slow analogue-tape fade, which would corrupt many bytes in place.
- **No benchmark against par2 or any other tool.** The "why not" section is
  reasoned from their documented behaviour, not measured.
- **Synthetic tests only; no compatibility claim.** All tests and measurements use
  generated data. Nothing has been verified against any particular drive, library,
  vendor tool or other FEC implementation, and the frame format is a prototype
  that is not standardised or frozen.