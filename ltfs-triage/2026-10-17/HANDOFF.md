# HANDOFF - LTFS triage (updated 2026-10-23)

History: `HANDOFF-2026-10-17-initial-403-blocker.md` (API 403 receipts in `raw/`, `collect.sh` never rerun).

## Current deliverables
- `TRIAGE.md` / `triage/ALL.csv`: 72 open non-PR issues (snapshot 2026-10-17T09:03Z), 23 open PRs kept separate. Classes: repro-mhvtl 5, hardware-only 20, question/support 40, stale/duplicate 5, already-fixed-upstream 2.
- `triage/TOP5.md`: final ranking, **4 of 5 slots** (#333, #106, #535 weak, #305 large). No fifth qualifies.
- `AUDIT.md`: real audit run (`python3 -I audit.py`), 72 = 72 IDs, CSV agrees.
- Root `TOP5.md` is now only a pointer.

## What changed vs the 2026-10-17 draft
- Source: fresh read-only `git clone` of upstream, HEAD efc9e3a (same as the snapshot); open PR heads fetched read-only via `refs/pull/N/head`. No upstream writes, no API calls.
- #541: real on main, but OPEN PR #603 already fixes it (`return fuse_failed ? 1 : ret;`), so it is excluded and **no `fixes/541.patch` was made** (your condition: only if not already fixed there). Open question: if #603 is not expected to merge soon, a standalone 1-line fix plus harness is possible; say so and I will do it.
- #598 and #414 are now already-fixed-upstream with commits (ba1695e / PR 599; 7d5c5af / #415). #522: closed PR 660 is NOT merged; symptom unread.
- #392/#513/#661/#650 are covered by open PRs 658/659/662/651 (diffs read).

## NOT DONE / limits
- Nothing built or run; no bug reproduced or fixed; no hardware claim. Repro plans use the file backend, not mhvtl (needs a kernel module; mhvtl docs not re-read).
- Maintainer comment threads (e.g. #535's 12 comments, #106 "historical issue") were not read. #305 recovery path not fully traced. #650 matched to PR 651 by title and diff only.
- Non-repro rows are still title/label-based, low confidence. ltfs_fsops.c was read only incidentally.
- User-requested layout: files live under `ltfs-triage/2026-10-17/triage/`, not a repo-root `triage/`.

## Reproduce
`python3 -I build.py <snapshot.json>` then `python3 -I audit.py` (run from this folder; snapshot is the parent-supplied file, kept out of Git).

## Lead review
Pick which of #333 / #106 to implement; #106 needs a maintainer answer on the "historical issue" before any patch.

## Update: #333 fix drafted and proven (2026-10-23)
`fixes/333.patch`, `fixes/333/` (shim, repro script, before/after outputs), `FIX-333.md`. Before: FAIL (A and B); after: PASS; no-fault control PASS on both builds. Caveat: stock ltfs never sets `index_cache_path`, so the bug is only reachable by external users of that field. Nothing sent upstream. Not run: real tape backends, LTFS-EE.
