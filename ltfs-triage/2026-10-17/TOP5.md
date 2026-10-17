# TOP5 - LTFS candidates (snapshot 2026-10-17)

**NO bug was reproduced or fixed in this research job. No hardware behavior verified.**

**Shortfall: 1 of 5 ranked. NOT DONE for the rest.** Only #541 had its source actually read. Other leads (#333, #414, #106, #305, #308, #535) were not read, so they are not ranked. Maintainer comment threads were not supplied (#541 shows 0 comments, so no ruling exists to cite).

RANKED: #541 [ltfs returns 0 even on failed mount](https://github.com/LinearTapeFileSystem/ltfs/issues/541)

- **Why**: small diff (one return-value path), user impact (scripts/automation see success on failed mount), no ruling against it in supplied data.
- **Source read** (pinned [main.c@efc9e3a](https://github.com/LinearTapeFileSystem/ltfs/blob/efc9e3a5c498504c03c2f1d0b931bae058922104/src/main.c#L1246-L1272)): L1246 `ret = fuse_main(...)`; L1247-1249 only logs 14123W; L1254 `ret = ltfs_set_signal_handlers();` overwrites it; L1272 `return ret;`. So a non-zero fuse_main result is lost unless the signal handler call fails. Reasoned from reading, not run.
- **Coverage check**: not referenced by any of the 23 open PRs' text. Open PRs [#655](https://github.com/LinearTapeFileSystem/ltfs/pull/655) and #603 (FUSE 3) touch the same exit path (FUSE 3 loop-error exit codes), so **conflict/overlap risk with #603/#655 - lead should check before any fix**. Not demonstrably fixed upstream (no merged-state data in snapshot).
- **Safe minimal repro sketch (not run)**: with the file backend or mhvtl (see mhvtl README: [pinned 852b78a](https://github.com/markh794/mhvtl/tree/852b78a7ae3add53ffdc418add5f14999762d788)), make fuse_main fail (e.g. mountpoint option rejected or no /dev/fuse in a container), run ltfs, check `echo $?`. Expect 0 per the reading above.
- **Acceptance caveat**: past rulings (e.g. rejected speculative changes) were not available in the snapshot: UNVERIFIED.

## Excluded (checked against snapshot)
- #392/#513/#661: covered by OPEN PRs 658/659/662 (bodies' Fixes refs). #650: OPEN PR 651. #598: possibly fixed by closed PR 599, merge state unknown, so no already-fixed claim.
- FIFO/device hangs and unreadable-subdirectory exit statuses: unranked hypotheses, not in this snapshot's issue set.
