# tapefec — format and design

A streaming, forward-error-correction framer. `tar | tapefec encode > file.tapefec`
survives isolated bad tape blocks; `tapefec decode < file.tapefec` restores the
original bytes, reporting what it could not restore.

Status: **prototype, format version 2 by default, not frozen.** Nothing here is
standardised. The decoder also accepts legacy v1 streams, whose parity matrix
differs as described in §2.2; the field layout is unchanged.

---

## 1. Framing

The output is a flat sequence of **frames**. Every frame starts with a fixed
40-byte header, immediately followed by its payload. All multi-byte integers are
little-endian.

### 1.1 Frame header (40 bytes)

| offset | size | field | meaning |
|-------:|-----:|-------|---------|
| 0  | 8 | `magic`         | `b"TAPEFEC\x1a"` — the resync marker |
| 8  | 1 | `version`       | `2` by default; legacy `1` also accepted |
| 9  | 1 | `frame_type`    | `0`=header, `1`=data, `2`=parity, `3`=trailer |
| 10 | 2 | `stream_id`     | u16, chosen by the encoder, must match across the stream |
| 12 | 4 | `frame_index`   | u32, monotonic over *every* frame including header/trailer |
| 16 | 4 | `block_id`      | u32, see §1.3 |
| 20 | 4 | `payload_len`   | u32, number of payload bytes that follow the header |
| 24 | 4 | `payload_crc32` | u32, CRC-32 of the payload bytes |
| 28 | 4 | `group_data_len`| u32, data blocks in this block's parity group (0 on header/trailer) |
| 32 | 4 | `reserved`      | 0 |
| 36 | 4 | `header_crc32`  | u32, CRC-32 over header bytes `0..35` (everything above it) |

Frame size on the wire is `40 + payload_len`.

`frame_index` exists for humans and for gap visualisation. It is **not** used to
place data: a dropped span breaks it, so `block_id` is what the decoder uses for
recovery. The decoder does use it as a plausibility bound on other header fields,
because every block occupies its own frame: a data or parity frame whose
`block_id` is not below its `frame_index` is impossible and is skipped, and a
trailer whose `total_length` exceeds `max(frame_index, frames read) * block_size`
is treated as damaged. These checks reject only internally inconsistent headers;
a stream that consistently declares a huge `frame_index` is not bounded by them.

### 1.2 CRC choice

Both CRCs are **CRC-32/ISO-HDLC** (the "CRC-32" of `zlib.crc32`, reflected,
poly `0xEDB88320`, init `0xFFFFFFFF`, final xor `0xFFFFFFFF`). CRC-32C
(Castagnoli) would be a better choice on tape hardware — tape CRC field widths
tend to be multiples of 16 bits and the Castagnoli polynomial is the magnetic-
media favourite — but CRC-32C is not in the Python standard library, and a
table-driven pure-Python CRC-32C over every payload would have been the slowest
thing in the program. `zlib.crc32` is a C-speed implementation in the stdlib.

This is a **detection** CRC, not a security primitive. A 32-bit CRC sees a block
as intact with probability ≈ 1 − 2⁻³² for random damage. It is not protected
against deliberate tampering.

### 1.3 Frame types

**`0` — stream header.** First frame of every stream. Payload is 16 bytes:

| offset | size | field |
|-------:|-----:|-------|
| 0  | 2 | `k` — data blocks per parity group (1..255) |
| 2  | 2 | `m` — parity blocks per group (1..255) |
| 4  | 4 | `block_size` in bytes (1..2³²−1) |
| 8  | 4 | `flags`     | reserved, 0 |
| 12 | 4 | reserved, 0 |

The stream is self-describing: `decode` needs no command-line knowledge of `-k`,
`-m` or `--block-size`.

**`1` — data block.** `block_id` is the **absolute data-block ordinal**
`0, 1, 2, …`. Group `g = block_id // k`, position in group `= block_id % k`.
`payload_len == block_size` for every data block except possibly the last one of
the stream.

**`2` — parity block.** `block_id` is the **absolute parity-block ordinal**.
Group `g = block_id // m`, position in group `= block_id % m`. `payload_len` is
always `block_size`. Every group has exactly `m` parity blocks, including a short
final group.

**`3` — trailer.** Always the last frame. Payload is 40 bytes:

| offset | size | field |
|-------:|-----:|-------|
| 0  | 8  | `total_length` — u64, exact byte length of the original stream |
| 8  | 32 | `sha256` of the original stream |

`decode` recomputes SHA-256 over everything it emitted and compares. The trailer
is an *integrity check on the reconstruction*, not a substitute for the parity.

### 1.4 Resynchronisation

A decoder scans forward for `magic`, then validates `header_crc32`. A 32-bit CRC
over a 40-byte header gives a false-accept probability of ~2⁻³² per candidate
position, so random payload bytes essentially never produce a plausible frame
header. When the header CRC fails, the decoder advances the scan by one byte and
looks again — so garbage, an inserted run of junk, or a partially erased frame
costs at most a resync, never the rest of the stream.

Consequence: **damage is contained to whole frames.** Junk between frames is
discarded and counted; junk *inside* a payload is caught by `payload_crc32` and
that frame becomes an erasure.

### 1.5 Overhead

A group of `k` data blocks of `b` bytes is written as `k + m` frames of
`b + 40` bytes, carrying `k·b` bytes of original data. Relative overhead:

```
overhead(k, m, b) = ((k+m)(b+40)) / (k·b) − 1
                  = m/k  +  40·(k+m)/(k·b)
                  = redundancy  +  framing
```

plus one 56-byte stream-header frame and one 80-byte trailer frame for the whole
stream (negligible; `136/L` for a stream of `L` bytes).

For the defaults `k=20, m=4, b=65536`:

| component | value |
|---|---|
| redundancy `m/k` | **20.000 %** |
| framing `40·(k+m)/(k·b)` | **0.0732 %** |
| **total (large stream)** | **20.073 %** |

Sanity check against the real numbers: 1 310 720 bytes encode to 1 573 960
bytes, i.e. 20.084 % — the extra 0.011 % is the header and trailer.
`test_overhead_matches_the_documented_formula` pins this.

| k | m | b | overhead |
|---|---|---|---|
| 20 | 2 | 64 KiB | 10.07 % |
| 20 | 4 | 64 KiB | 20.07 % |
| 32 | 4 | 64 KiB | 12.54 % |
| 20 | 8 | 64 KiB | 40.15 % |

Redundancy is `m/k`, not `m/(k+m)`: you add `m·b` parity bytes for every `k·b`
original bytes. A 20 % overhead is what "any 4 of every 24 blocks recoverable"
costs at this group size. Framing is negligible at 64 KiB but is 2.5 % at
`b=1024`, so the block size is a real design dial, not a detail. Bigger `k`
buys cheaper overhead (larger groups amortise framing, and the failure is
averaged over more blocks) at the price of a longer repair group and more
memory.

---

## 2. Reed–Solomon parity over GF(2⁸)

### 2.1 The field

`GF(2^8)` with the primitive (irreducible) polynomial **0x11D**, i.e.
x⁸ + x⁴ + x³ + x² + 1 — a common choice for byte-oriented Reed–Solomon
codes. The generator element is **α = 0x02**, which is primitive under 0x11D.

Implemented with log/antilog tables (`_gf_exp`, `_gf_log`) built once at import,
plus one 256-byte `bytes.translate` table per constant (`_MULTAB[c][x] = c·x`).
No dependencies; numpy is an optional accelerator (§2.4) over the same tables.

### 2.2 Construction: default v2 Cauchy, legacy v1 Vandermonde

For a group of `k` data blocks `d₀ … d_{k−1}` and `m` parity blocks,
parity is computed independently at each byte position:

```
p_i = Σ_{j=0}^{k−1} d_j · G[i][j]          for i = 0 … m−1
```

The default format version 2 uses a Cauchy matrix over the field in §2.1:

```
x_i = i                    for i = 0 … m−1
y_j = m + j                for j = 0 … k−1
G[i][j] = inverse(x_i XOR y_j)
```

`XOR` is field addition. With `k+m ≤ 256`, all selected field elements are
distinct and the two sets are disjoint. Thus every denominator is nonzero,
and every square submatrix is invertible. Together with the unchanged data
blocks, this gives a systematic MDS erasure code: any `m` missing data/parity
frames in a group can be recovered, provided surviving frames are intact.
For `k=4, m=2`, row zero is `[142, 244, 71, 167]`, not a plain XOR row.

The decoder selects the matrix using the stream's version. Legacy v1 uses
`G[i][j] = α^(i·j mod 255)`, whose row zero is all ones. That construction is
**not MDS for every supported parameter pair**; some erasure patterns at
`k=10, m=6` yield a singular repair matrix. The decoder reports those groups
as unrecoverable and continues. V1 compatibility does not inherit the v2
any-`m` guarantee. The API's explicit `version=1` option exists for compatibility
fixtures; the CLI encoder writes v2.

Byte `t` of each block forms one codeword
`(d₀[t], …, d_{k−1}[t], p₀[t], …, p_{m−1}[t])`. CRC failures identify erased
frames. Neither matrix repairs undetected corruptions or protects a missing
stream header; the trailer remains necessary for final integrity verification.

### 2.3 Why erasure decoding is enough

The CRC already tells the decoder exactly which frames are unusable, so this
implementation never runs a Berlekamp–Massey / Chien / Forney error locator.
It solves for missing blocks directly.

Let `E` be the set of missing/corrupt **data** block positions (`|E| = e`) and
`P` the set of parity blocks that survived. `p_i = Σ_j d_j·G[i][j]` gives, for
each surviving parity row `i ∈ P`, one equation over the unknowns:

```
Σ_{j∈E} d_j · G[i][j]  =  p_i  ⊕  Σ_{j known} d_j · G[i][j]        ( =: b_i )
```

`A = G[P][E]` is `e × e`. For default v2, `G` is MDS, so every such submatrix is
invertible, so `u = A⁻¹b` where `u = (d_j)_{j∈E}`. `A⁻¹` is computed once per
group by Gauss–Jordan elimination over GF(2⁸) (at most `m ≤ 255` — in practice
`m = 4`, a 4×4 inversion).

**Feasibility condition.** With `e` missing data blocks and `r = m − |P|`
missing parity blocks, the system is solvable iff `e ≤ |P|`, equivalently
`e + r ≤ m` — *total blocks lost from the group must not exceed `m`*. Losing a
parity block costs exactly as much repair capacity as losing a data block,
which is why the encoder always writes the full `m` parity blocks and the
decoder counts both kinds in one budget.

### 2.4 Fast whole-block arithmetic

Naively this is `m·k` GF multiplies **per byte position**: 80 multiplies ×
65536 positions per 1.25 MiB group, which in Python is minutes per group. Two
observations make it fast without leaving the stdlib:

1. Multiplying a whole block by a constant is `bytes.translate(TABLE)` — one C
   loop over 64 KiB.
2. XOR of whole blocks is XOR of the corresponding Python big integers —
   `int.from_bytes(...)`, one C big-int XOR, `to_bytes(length, 'big')`.

So parity row `i` is
`acc = XOR_j int.from_bytes(d_j.translate(T_j), 'big')`, which touches each
block `k` times at memory speed. Decode is the same shape plus one small
matrix inversion. Measured throughput is in `README.md` §Throughput.

**Optional numpy engine.** Profiling the pure encoder shows the big-integer
round trip (`int.from_bytes`, XOR, `to_bytes`) costing more than the
`translate` calls themselves. When numpy is importable, `_np_combine` computes
the same `rows[i] = XOR_j c_ij·d_j` differently: for input block `j` it builds
a 256-entry table whose entry `x` packs `c_0j·x, c_1j·x, …` for up to 8 output
rows side by side in one 1/2/4/8-byte word (taken from `_MULTAB`, so the field
arithmetic is literally the same table), does one `np.take` of block `j`
through it, and XORs the words into an accumulator. That is one gather per
input block per 8 rows, instead of one translate plus a big-integer conversion
per (row, block) pair. Encode calls it with `G`; repair calls it twice, with
`[G[P][known] | I]` over the known data and surviving parity to form `b`, then
with `A⁻¹` over `b`. GF(2⁸) arithmetic is exact, so the two engines cannot
differ except by a bug, and `test_tapefec.py` holds them to golden stream
digests captured from the pure encoder before the numpy engine existed.
Engine choice (`--engine`, `TAPEFEC_ENGINE`, `auto` by default) never reaches
the wire.

### 2.5 Padding the final group

If the input length is not a multiple of `k·b`, the last group has `k' < k`
data blocks. The encoder still emits `m` parity blocks, computed over the `k'`
real blocks plus `k − k'` **virtual all-zero blocks**. Zero-padding rather than
shrinking the code is deliberate: it keeps the full `m`-block repair budget for
the last group, and zero-padding costs nothing at encode time (skipped
entirely in the loop). The decoder learns `k'` from the `group_data_len` field
that every frame in the group carries, so nothing in the stream header has to
predict it — which is why `flags` can stay reserved.

---

## 3. Decode algorithm

```
scan frames, resyncing on magic + header CRC
  ↓ first valid frame must be frame_type 0 → k, m, block_size
  ↓ every later frame whose stream_id differs is FOREIGN: counted, discarded,
    never used to fill a slot   (a wrong-stream frame must not be able to
    masquerade as a real block)
group frames by (block_id // k) for data, (block_id // m) for parity
  ↓ ordinal gaps reveal groups that vanished entirely → emit zeros, unrecoverable
  ↓ a CRC-valid parity payload whose length is not block_size, or a data
    payload longer than block_size, cannot be a codeword symbol: erasure
for each group, in order:
    good data  = frames present AND payload_crc32 ok
    E          = positions in 0..k'−1 that are absent or CRC-failed
    P          = parity positions present AND payload_crc32 ok
    if |E| > |P|: mark those blocks unrecoverable, emit b zero bytes each
    else:        u = A⁻¹b → repaired blocks, emit them
a group is finalized as soon as all k' data and m parity frames are in and
  intact (nothing later can change it; a duplicate is dropped), otherwise
  when a frame of a later group, or the trailer, arrives
hold back the last k+1 blocks in a delay line
  ↓ the trailer's total_length is the only thing that says how long the final
    block is, and it arrives last; the delay line bounds memory at ~2(k+m)·b
    while letting the output be trimmed (too long) or padded (too short)
compare SHA-256
```

**Output contract.** `decode` always writes every byte it could reconstruct, at
the original stream length. Unrecoverable ranges are written as zeros so
downstream `tar` sees the right stream *length* and fails cleanly instead of
silently losing alignment; the ranges are listed on stderr as JSON.

Exit status:

| code | meaning |
|---|---|
| 0 | every byte recovered **and** the trailer's SHA-256 matched |
| 1 | something was unrecoverable, the digest disagreed, or the trailer is gone so nothing could be verified |
| 2 | not a tapefec stream, or bad arguments |

The trailer requirement is deliberate: a truncated stream that lost nothing
recoverable is still not a success, because "I got all the bytes I was given"
and "I got the bytes you promised" are different claims. `--strict` additionally
fails when any block needed repair. `--quiet --strict` may suppress JSON
when verified repair is the only cause of failure; unrecoverable blocks, digest
failure, and a missing trailer still produce JSON. Known trailer EOF bounds
unrecoverable byte ranges. Without a trailer those ranges retain block-sized
estimates, and wholly missing trailing groups cannot be inferred.

---

## 4. What this format does NOT protect against

These are deliberate, not oversights. Stating them is part of the design.

1. **More than `m` damaged blocks in one group.** With `k=20, m=4`, five dead
   frames inside the same group of 24 kills that group, full stop. Groups are
   independent, so damage spreads across groups is far cheaper than clustered
   damage. There is no second-level parity, no cross-group code.
2. **Undetectable corruption.** If a payload is damaged in a way that preserves
   CRC-32, the decoder believes it. It will be used as a *known* symbol and will
   poison the repair of the other blocks in that group. CRC-32 + RS is not a
   substitute for a cryptographic digest; the trailer SHA-256 catches the
   failure *after* the fact, at whole-stream granularity.
3. **Damage to the CRC fields themselves.** A corrupt `header_crc32` makes the
   frame unreferenceable and a corrupt `payload_crc32` makes it an erasure. Both
   degrade to "one more lost block", which is the intended behaviour, but they
   are not covered by any parity.
4. **Lost stream header or trailer.** No `k`/`m`/`block_size` without the header
   frame; no length or digest without the trailer. If either is lost the decoder
   reports it and exits non-zero, but the parity still recovers the data bodies.
5. **Reordering across the tape.** Frames are self-identifying (`block_id`), so
   the decoder *tolerates* frames arriving out of order within a group, but it
   does not reorder across groups and there is no mechanism to seek backwards.
   This is a streaming framer, not a random-access index.
6. **Hostile input.** `payload_len` is validated against a cap derived from the
   header frame before allocation, but this is a prototype: there is no fuzzing
   campaign and no resource limits. Do not point it at untrusted data yet.
7. **Encryption, compression, authentication.** None. Pipe `zstd`/`age` in or
   out as you like; that is the unix way.
8. **Physical tape semantics.** It does not write filemarks, does not know about
   `mt`, does not drive a changer, and has no idea what an "EOT" is. It reads
   bytes and writes bytes.

---

## 5. Why not par2 / redupe

- **par2** (`par2cmdline`) needs random access and a seekable file: it reads the
  input twice to build a recovery set. A stream cannot be rewound. It also
  works on whole files, which forces `tar` → `par2` to spill the whole archive to
  disk first — exactly the step you were trying to avoid. A commenter on the HN
  thread this was built from reached for `par2` as the closest thing
  ([HN thread](https://news.ycombinator.com/item?id=30099540)), and other
  comments there argue that the drive's own ECC plus a whole-file `par2` is a
  different tool for a different job, and that interleaving is the part that
  matters.
- **redupe** was also mentioned in that discussion. It has not been evaluated
  here, and this document makes no claim about its current availability.

---

## 6. Sources

- Format motivation and prior art: the Hacker News thread
  <https://news.ycombinator.com/item?id=30099540>, read 2026-10-13. It discusses
  the original request, `par2`, interleaving and drive-level ECC.
- Reed, I. S. and Solomon, G., "Polynomial Codes Over Certain Finite Fields",
  *Journal of the Society for Industrial and Applied Mathematics* 8(2), 1960,
  pp. 300–304, DOI 10.1137/0108018
  (<https://epubs.siam.org/doi/10.1137/0108018>).
- Berlekamp, E. R., *Algebraic Coding Theory*, McGraw-Hill, 1968
  (<https://math.berkeley.edu/~berlek/pubs/books.html>, the author's own
  bibliography).
- Lin, S. and Costello, D. J., *Error Control Coding*, 2nd edition, Pearson,
  published May 28, 2004, copyright 2005
  (<https://www.pearson.com/en-us/subject-catalog/p/redirected-product/P200000003536?view=educator>,
  publisher page).

The three references above are background reading only. Their bibliographic
details were checked against the publisher or author pages linked above during
publication; the full texts were not read for this document, so no specific
algorithm, construction or claim in this design is attributed to them. The
original development did not retrieve any Reed–Solomon reference. The tests in
`test_tapefec.py` (GF identities, exhaustive round trips) are implementation
checks: they are not a mathematical proof and not an independent validation
against any source.
