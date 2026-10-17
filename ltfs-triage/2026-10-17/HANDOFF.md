# HANDOFF - LTFS triage 2026-10-17 (current)

Earlier access-blocker history: `HANDOFF-2026-10-17-initial-403-blocker.md` and `raw/` (403 receipts, kept as-is; `collect.sh` is NOT a successful collection and was not rerun).

## DONE
- Parent-supplied public snapshot (collected 2026-10-17T09:03Z; zip sha256 1cff6906...cb230, member sha256 ac27a26f...fb1f5b, both recomputed OK). Input kept outside Git.
- Counts: **72 open non-PR issues** (95 mixed open - 23 open PRs); open PRs: **23**. Classes in TRIAGE.md header (already-fixed-upstream = 0: no evidence available).
- Closed window: see `census.json` (closed_at >= 2025-10-17 filtered locally; mixed closed response 84, closed-PR page 100 with min updated 2022-12-25, Link next present but beyond window). Closed issues and PRs in the window are counted from the supplied normalized items only.
- TRIAGE.md, TOP5.md (1 ranked), AUDIT.md (real run).
- Sources: LTFS commit efc9e3a5c498504c03c2f1d0b931bae058922104; mhvtl commit 852b78a7ae3add53ffdc418add5f14999762d788 (docs not read this pass).

## NOT DONE / limits
- Snapshot is normalized selected fields, not raw HTTP. src/libltfs/ltfs_fsops.c was omitted by the parent's guard; not read.
- Only #541's source was read. 4 more ranks not filled. No maintainer comments or merged-state data, so #598/#522 stay UNVERIFIED.
- mhvtl capability was not checked against docs; repro sketches are reasoned only. No bug reproduced/fixed; no physical-drive claim; no CI ran.

## Reproduce
`python3 -I build.py <snapshot.json>` (writes census.json, TRIAGE.md), then `python3 -I audit.py`.

## Lead review
Overlap of #541 with open PRs #603/#655; class judgments for non-repro rows are title/label-based (low confidence).
