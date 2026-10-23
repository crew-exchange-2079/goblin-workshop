# AUDIT

Command: `cd ltfs-triage/2026-10-17 && python3 -I audit.py` (2026-10-23)

```
snapshot issues 72 rows 72 unique 72
set equal True
class rows 72 distinct classes ['already-fixed-upstream', 'hardware-only', 'question/support', 'repro-mhvtl', 'stale/duplicate']
ranked [333, 106, 535, 305] exist True in open PR set []
issue/PR overlap set()
csv rows 72 ids equal True
{'repro-mhvtl': 5, 'hardware-only': 20, 'question/support': 40, 'stale/duplicate': 5, 'already-fixed-upstream': 2}
```

Hashes:
```
5b47caa3fa37b83e636ec9907baa0a64d867ef2374925a229e53af413010f7f8  TRIAGE.md
025e28ea2a9cadf2660221fe0ef9c14cbb9f3d15348b373b4672259067c99e51  triage/TOP5.md
9b4d01aa63205d4a9ca6149393e922cf6ac794ef86584acfc609020a3fb3f7ec  triage/ALL.csv
0255407818f5b59868a35e7d835a1f3bea62128bbfb3346fb187c576b0623dbb  census.json
9aa8b1f3ab3e70448eef416e05e8dfd62d7f82909bcb8a3a1d79cfd25a747f53  build.py
5fa30f33fd3252fd76efbd642c8a35bea85fbcea04a6b005c1828114d5fef688  audit.py
```
Ranked-vs-open-PR exclusion beyond the script's ID check was done by reading PR diffs (see triage/TOP5.md). No CI ran.
