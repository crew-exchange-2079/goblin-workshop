# Bug sweep findings (branch `sweep-20261022`)

Scope: parsers and validators — `tapefec`, `rack-parts` (config + STL reader),
`tape-exporter` (sg_logs / SSC / tapeinfo parsers, config, command runner),
`ltfs-index-validator`, and the mhvtl harness. Public data only, nothing submitted.

Every CONFIRMED item has a test in `tests/sweep/` that fails on the
test-only commit (`pytest tests/sweep -x`) and passes after the fix commit.

## CONFIRMED

| # | Where | Bug | Test |
|---|-------|-----|------|
| 1 | `tapefec.decode_stream` | A frame's `group_data_len` is trusted even when it exceeds the stream's `k`. A data frame claiming 3 blocks in a k=2 stream makes the decoder emit 3 blocks (12 bytes) for a 2-block group; a value like 2**32-1 loops ~4 billion times. | `test_tapefec_decode.py::test_group_data_len_above_k_is_not_trusted` |
| 2 | `tapefec.decode_stream` | Trailer `total_length` (u64) is unchecked. A 2-frame stream whose trailer claims 2**40 bytes makes `pad_missing_tail` emit zeros without bound (hang / memory growth). | `::test_trailer_total_length_must_be_consistent_with_frame_count` |
| 3 | `tapefec.decode_stream` | `block_id` (u32) is unchecked. One data frame with `block_id=2**31` drives `zero_group()` for billions of non-existent groups. | `::test_block_id_cannot_exceed_frame_index` |
| 4 | `rackparts.mesh.analyse` | A closed mesh with an `inf` coordinate (ASCII `vertex 0 0 inf`) reports `ok: true` with `volume_mm3: nan`; `nan <= 0` is false so the volume check never trips. | `test_rackparts_mesh.py::test_infinite_coordinate_is_not_ok` |

### Fixes (smallest change each)

* 1: out-of-range `group_data_len` (0 or > k) is treated as "full group", same as the existing 0 case.
* 2: a trailer with `total_length > max(trailer.frame_index, frames_read) * block_size` is treated as damaged (existing "trailer damaged" path).
* 3: a frame with `block_id >= frame_index` is skipped; each block occupies its own frame, so it cannot be genuine.
* 4: `analyse` raises `MeshError` on any non-finite vertex coordinate.

Fixes 1–3 were later replaced by an exact layout check on every frame header
(`tapefec/DESIGN.md` §1.1), after fuzzing showed that a CRC-valid frame from
another stream could still crash the decoder or fill a slot. Fix 1 now ignores
the frame instead of assuming a full group.

Known limit of fixes 2 and 3: they reject *internally inconsistent* headers only.
A hostile stream that self-consistently declares a huge `frame_index` still makes the
decoder pad zeros, because the format itself lets a trailer assert a length that the
frames present cannot back. Closing that needs a format-level or caller-supplied cap.

## REJECTED

* **`read_stl` reads the whole file with no size cap.** True, but no bound is defined by the project (ASCII STL has no natural limit; binary is capped at 2M facets ≈ 100 MB). Any fix is a policy choice, and a test would need a multi-GB file. Not a provable defect.
* **`parse_sg_logs_error_counters` keeps the first of two conflicting duplicate lines** (the SSC parser instead drops conflicts). Inconsistent, but sg_logs never emits duplicates and no input shows wrong data from real output. Design difference, not a bug.
* **`parse_tapeinfo_text` sets `parsed=True` when a known key has an unparseable value** (all fields stay `None`). Offline-only helper, cosmetic.
* **Huge or oversized counters** (> 2**64-1, unicode digits, NUL, CRLF, tabs) in sg_logs pages: probed, handled — reported as missing, never zero.
* **ltfs-index-validator** (empty, BOM, wrong declared encoding, trailing NUL, DOCTYPE/entity, size/depth/node limits): probed, all refused with fixed codes. No bug found.
* **rack-parts config validation**: 20,000 random type/range mutations of the example configs (huge ints, NaN-ish, wrong types, short/long lists) produced only `ConfigError`, never another exception. No bug found.
* **tape-exporter config/command runner**: exact-shape argv check, bounded read, timeout and kill paths reviewed; no input-handling defect found.
* **Binary STL with trailing bytes or `solid` header text** falls to the ASCII path and ends in a `MeshError`, not a wrong result.

## Test run

`pytest` is not installed by default here; used `pip install pytest`.
No top-level suite exists, so each project's suite was run: tapefec 84 passed,
rack-parts 69 passed (8 skipped), tape-exporter 126 passed (1 skipped),
ltfs-index-validator 20 passed, mhvtl-status 21 passed, tests/sweep 5 passed.
