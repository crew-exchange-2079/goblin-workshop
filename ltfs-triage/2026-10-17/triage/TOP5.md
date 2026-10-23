# TOP5 (final) - LTFS candidates, upstream main @ efc9e3a, read 2026-10-23

**NO bug reproduced or fixed in this research job. Nothing was built or run; repro plans are reasoned sketches.** Source read from a fresh read-only clone of upstream (pinned links below); open PR heads fetched read-only via `refs/pull/N/head`. No upstream comments, PRs or pushes. No maintainer comment threads were read (not available in the snapshot or clone), so "acceptance" below is inferred from in-source maintainer TODOs and issue text only.

**Shortfall: 3 credible candidates plus 1 weak one (4 of 5 slots). No fifth qualifies**: #308 is a design discussion and #541/#414/#598 are excluded (below).

Repro note: a mhvtl virtual library needs a kernel module and was not available/used; every sketch uses the LTFS **file backend** (`-e file`, no hardware, no kernel module) or a libltfs-level unit test, which is the easier no-hardware route. mhvtl docs were not re-read in this pass.

RANKED: #333
RANKED: #106
RANKED: #535
RANKED: #305

## 1. #333 - index write aborts when the index-cache write fails  [issue](https://github.com/LinearTapeFileSystem/ltfs/issues/333)
- **Real bug (source read):** [xml_writer.c L108](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_writer.c#L108) returns -1 once `errno_fd` is set; [L123-129](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_writer.c#L123-L129) sets `errno_fd = -LTFS_CACHE_IO` on a cache `arch_write` failure and returns -1, so libxml2 stops emitting and the rest of the index never reaches tape. [xml_writer_libltfs.c L894-897](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_writer_libltfs.c#L894-L897) then reports the error and skips `tape_write_filemark` ([L899-L905](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_writer_libltfs.c#L899-L905)). The close callback [xml_writer.c L158-170](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_writer.c#L158-L170) has the same pattern. Issue's own expected behavior: finish writing the index to tape, report the cache error afterwards.
- **No-hardware repro plan (not run):** file backend volume, point the index cache at a filesystem that fails writes (tiny tmpfs, or a path whose fd hits ENOSPC), mount, create enough files to make a multi-block index, unmount/sync; expect a truncated index on the tape image and no trailing filemark. Unit-level alternative: drive `xml_output_tape_write_callback` against the file backend with `ctx->fd` on `/dev/full`.
- **Fix size:** small-to-medium, ~20-30 lines in 2 files: on cache failure drop the cache fd and remember the error, keep writing tape blocks, write the filemark, return the cache error afterwards (must decide whether the tape index still counts as committed; that is a maintainer call).
- **Open-PR overlap:** none on these functions. PR [#609](https://github.com/LinearTapeFileSystem/ltfs/pull/609) touches xml_writer.c only in the time-format function; PR #580 is a formatting/ignorefile change touching many files (textual conflict risk only).
- **Maintainer activity:** xml_writer.c last changed 2026-06-15 (#592); libltfs memory-leak work merged 2026-03 (#575). Issue is maintainer-style and 4+ years old, 0 comments.

## 2. #106 - US (0x1f) in a file name is rejected  [issue](https://github.com/LinearTapeFileSystem/ltfs/issues/106)
- **Real, acknowledged in source:** [pathname.c L442](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/pathname.c#L442) `_pathname_valid_in_xml` rejects 0x1f; [xml_reader_libltfs.c L117-127](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_reader_libltfs.c#L117-L127) and [L140-149](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_reader_libltfs.c#L140-L149) carry a maintainer TODO: "Need to remove US (0x1f) ... Now LTFS rejects US because of a historical issue (See Issue #106)". Expected per issue: accept and write `aaa%1faaa` with `percentencoded` ([writer L153-159](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/xml_writer_libltfs.c#L153-L159)).
- **No-hardware repro plan (not run):** libltfs unit test calling the pathname validation on "a\x1fb" (expect -LTFS_INVALID_PATH today); or file-backend + FUSE mount + `touch $'mnt/aaa\x1faaa'` (needs /dev/fuse).
- **Fix size:** small code (3 sites: one condition each) but needs a round-trip test (write, remount, read back) and the "historical issue" is not explained anywhere I could read: **compatibility risk with existing tapes/other writers is the main acceptance uncertainty**.
- **Open-PR overlap:** PR #603 (and PRs stacked on it) edit xml_reader_libltfs.c, but only the NULL-check and realloc hunks seen; no change to the 0x1f logic. PR #613 touches pathname.c leak paths only.
- **Maintainer activity:** pathname.c last 2025-05-16 (#520); xml_reader_libltfs.c 2026-06-15.

## 3. #535 - file backend only mounts/formats LTO5  [issue](https://github.com/LinearTapeFileSystem/ltfs/issues/535)  (weak: may be by design)
- **Source read:** [filedebug_tc.c L1349-1380](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/tape_drivers/generic/file/filedebug_tc.c#L1349-L1380) `_sanitize_tape` accepts only cartridge types allowed for the emulated drive's generation; the drive type comes from the product id ([L601-608](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/tape_drivers/generic/file/filedebug_tc.c#L601-L608)) while defaults are LTO5 ([L580-583](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/tape_drivers/generic/file/filedebug_tc.c#L580-L583)). A config of cart_type L6 on an LTO5-class emulated drive is rejected as designed. Whether the real defect is a missing way to choose the emulated drive generation, a doc gap, or something else needs the issue's 12 comments (not read).
- **Repro plan (not run):** exact steps in the issue body (mkltfs -e file ... edit cart_type/density_code ... mkltfs -f).
- **Fix size:** unknown; a doc note is trivial, a config option is small-medium. **Open-PR overlap:** PR #620 touches filedebug_tc.c (error-type swap only); #580 formatting.
- **Maintainer activity:** file backend last changed 2026-06-15 (#592), 2025-08 (#529).

## 4. #305 - recovery from trailing incomplete index  [issue](https://github.com/LinearTapeFileSystem/ltfs/issues/305)  (not "small")
- Real gap per maintainer text (two layouts, `| index | data | (incomplete) index | eod`). Needs new recovery logic around the mount/scan path ([ltfs.c ltfs_mount L1571](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/ltfs.c#L1571), traverse L3756/L3839); I did not trace the full recovery path. Could be exercised with a hand-built file-backend image. Diff size large; acceptance depends on design. No open-PR overlap found.

## Excluded / resolved
| item | result |
|---|---|
| #541 | **Real on main** ([main.c L1246-1272](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/main.c#L1246-L1272): `ret = fuse_main()` then `ret = ltfs_set_signal_handlers()` L1254 overwrites it, `return ret` L1272). But **OPEN PR #603 already fixes it** (adds `fuse_failed` and `return fuse_failed ? 1 : ret;`); PR #655 refines the FUSE3 loop-end case. Excluded; no patch written. |
| #414 | **Already fixed**: commit 7d5c5af (#415, 2023-08-24); [tape.c L3616-3645](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/tape.c#L3616-L3645) frees `inner_buf` on all paths. |
| #598 | **Already fixed**: merged PR #599, commit ba1695e (2026-07-29), [ltfs_ordered_copy L413-432](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/utils/ltfs_ordered_copy#L413-L432). |
| #522 | Closed PR #660 (2-line fix) is **not merged** (not an ancestor of main; main unchanged). Symptom not read: UNVERIFIED. Possibly a fix-ready lead. |
| #392 #513 #661 #650 | Covered by OPEN PRs 658 / 659 / 662 / 651 (diffs read; #650 matched by title+diff, body not cross-checked). |
| #308 | Design discussion; forward traverse for rollback exists ([ltfs.c L1875](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/libltfs/ltfs.c#L1875)). Not a bug. |
| side note | tape_read_attr L3631: length field can reach 0xFFFF+4 > MAXMAM_SIZE (0xFFFF) -> possible small over-read from a malformed drive reply; unranked observation, not verified. |
