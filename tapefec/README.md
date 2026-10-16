# tapefec — streaming forward-error-correction for tar-to-tape

**A pure-Python, standard-library-only educational prototype. All testing is
against synthetic data; nothing here has been run against a physical tape, drive,
library or any vendor's hardware or software, and no compatibility is claimed.**

`torture-output.txt` records 800 trials of random synthetic frame damage in which
the decoder never reported "verified" for a wrong output. See `DESIGN.md` §2 for
how the Reed–Solomon code is built and `test_tapefec.py` for the current 84 unit
tests (the fixes behind them were also confirmed by 8 separately verified
regression reproductions). The torture and throughput outputs below are
historical measurements from before those fixes, not new runs.

**Reproduce (stdlib only, nothing installed):**
`python3 -m unittest test_tapefec && python3 torture.py`

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
compile.

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
the whole frame list); `encode` and `decode` are the bounded-memory paths.

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

Historical example from one development workstation (`nice -n 19 python3 bench.py --mb 200`),
k=20, m=4, 64 KiB blocks, one core; not re-measured after the later fixes and not
representative of other machines. Wall-clock there was dominated by filesystem jitter
(identical runs measured 0.09 s and 1.51 s), so the quoted figure is child
process CPU time.

| stage | MB/s |
|---|---|
| encode, CLI stdin→stdout | **128** |
| decode, CLI stdin→stdout | **546** |
| encode, in-process CPU | 131 |
| decode, in-process CPU | 612 |
| `rs_encode_group()` alone | 237 |
| `zlib.crc32` over the whole encoded stream | 9 864 |

Reading that honestly:

- **Decode is cheap** because decoding an undamaged stream does *no* Reed–Solomon
  arithmetic at all — it is CRC, a `bytes.find` resync scan and I/O. The RS step
  only runs for groups that actually lost blocks.
- **Encode is bounded by the parity step** (237 MB/s), which is the whole cost.
  It is not doing anything clever: `bytes.translate` (one C loop per block per
  parity row) plus big-integer XOR. 128 MB/s out of 200 MB/s of I/O and CRC means
  the arithmetic is ~40 % of the encode.
- **Pure Python may be adequate for a single stream.** Native LTO drive speeds are
  published by the vendors and are of the same order as the encode figure above, but
  no tape drive was used here, so no claim is made about keeping a drive streaming.
  It would not keep up with an NVMe-to-RAID array doing multi-stream writes, and it
  is not trying to.

**What a C or Rust port would cost.** The format is not the problem; the
`translate`+big-int trick is the problem. In C, the parity step is a table-driven
multiply-accumulate: `k*m = 80` GF(2⁸) multiplies per 64 KiB byte position,
~5.2 M byte-ops per 1.25 MiB group. With a 64 KiB lookup table and SIMD
(`_mm_shuffle_epi8` in C, `u8x16_shuffle` in Rust) this is a handful of
`pshufb`+`pxor` per 16 bytes and should run at **several GB/s** — CRC32 and
SHA-256 have hardware instructions on every x86-64 and ARMv8 CPU
(`crc32` + `sha256rni`), which alone put a floor of ~10 GB/s on the framing.
So: a good C implementation should land at **1–3 GB/s** encode and **5–10 GB/s**
decode, versus 128 / 546 MB/s here. A straightforward scalar port with no SIMD
should still beat 1 GB/s on encode because it avoids the translate/big-int
emulation entirely.

The honest engineering conclusion is that this should *not* be ported until
someone needs multi-stream tape writes: a Python file that anyone can read and
audit is worth a lot when the data is archival.

## Repository layout

| file | what it is |
|---|---|
| `tapefec.py` | the whole tool: framing, GF(2⁸)/Reed–Solomon, encoder, decoder, CLI |
| `DESIGN.md` | wire format, RS construction, overhead maths, and what the format does **not** protect against |
| `test_tapefec.py` | 84 unittest tests: GF identities, RS erasure patterns, round trips, damage |
| `torture.py` | 200 trials × 4 damage rates over a 20 MB stream |
| `torture-output.txt` | the torture table, pasted from a historical run |
| `bench.py` | throughput measurement |

## Format versions

The CLI encoder defaults to **format version 2 with a Cauchy parity matrix**.
The decoder also accepts legacy v1 Vandermonde streams. The v2 construction
recovers any `m` erased data/parity frames per group when surviving frames are
intact; legacy v1 has singular patterns for some parameters and does not share
that guarantee. V2 parity row zero is not generally a plain XOR. See DESIGN §2.2.
No wire layout or version is changed by the current reporting/validation fixes.


What is included:

- `DESIGN.md`: 40-byte frame header with offset table, systematic Cauchy erasure
  code over GF(2⁸)/0x11D for default format version 2, erasure-only decoding with
  the feasibility condition `erasures + lost_parity ≤ m`, overhead maths, and an
  explicit "does NOT protect against" list.
- `tapefec.py` (~900 lines, one file, `--help`, `-k`, `-m`, `--block-size`,
  `--stream-id`). Encode and decode are both streaming with memory bounded at
  about `2·(k+m)·block_size`.
- 84 unit tests: an original 68-test set plus later regression tests covering
  reporting, EOF handling and size validation.
- `torture.py` (200 trials per damage rate over 20 MB; table in
  `torture-output.txt`) and `bench.py`. Their published outputs are historical,
  and the C/Rust port cost above is an estimate, not a measurement.

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
- **RS encode is not optimised.** `m·k` block operations per group where a
  faster implementations would require separate work and measurement. The
  historical throughput examples above are not a new benchmark of these fixes.
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