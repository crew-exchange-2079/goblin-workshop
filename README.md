# goblin-workshop

This is the crew's public workshop for small open tools — a place where we build, share, and tinker with lightweight utilities that anyone is welcome to use, fork, or improve.

## Tools

- [`tape-exporter/`](tape-exporter/): read-only Prometheus exporter for Linux LTO/SCSI tape drives (MIT).
- [`tapefec/`](tapefec/): streaming forward-error-correction (Reed–Solomon) framer for tar-to-tape pipelines; pure-Python educational prototype (MIT).

## Tape tools

The tape tools here are open and vendor-agnostic, aimed at tape administrators. They are designed around public standards — T10 SCSI log pages and MAM attributes (read via sg3_utils), LTFS, and plain byte streams — and are developed from public specifications and synthetic data only. That describes design intent, not testing: operation across particular vendors, drives or libraries has not been verified, and each tool's own README states what it has and has not been tested against.
