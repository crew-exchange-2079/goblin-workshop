# ltfs-index-validator

Offline **schema-only** checker for LTFS 2.4.0 *full* index XML (unnamespaced, `version="2.4.0"`), using the
SNIA Annex B XSD (see `NOTICE`, `PROVENANCE.json`). Not LTFS compliance or certification.

    python3 -m venv .venv && .venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.txt
    .venv/bin/python validate.py --input fixtures/valid-empty.xml
    .venv/bin/python -m unittest discover -s tests -v

Exit codes: 0 `SCHEMA_VALID_ONLY`, 1 `INVALID`, 2 `UNSUPPORTED` (other version/namespace/root/non-UTF-8),
3 `ERROR` (bad input path/kind/size, missing schema or dependency, hash mismatch, usage). One bounded JSON
report (<=64 KiB) with input/schema hashes and fixed diagnostic codes plus line/sanitised path; rejected XML
values, filenames and raw parser errors are never echoed.

Platform: Linux only (needs `O_PATH` and `/proc`); elsewhere every input is refused as `ERROR`. Input is pinned with an `O_PATH|O_NOFOLLOW`
descriptor (no device driver is opened), checked as a regular file, then reopened via `/proc/self/fd/N` with inode identity verified;
paths resolving into `/dev` are refused. A non-UTF-8 XML encoding declaration is `UNSUPPORTED`.

Hardening: <=4 MiB read from a nonblocking no-follow regular file (no URLs, FIFOs, symlinks, directories, `/dev`);
UTF-8 only; depth<=64, nodes<=100000; DOCTYPE/entities refused; parser with no recovery, no huge tree, no DTD,
no default attributes, no entity resolution, deny-all resolver; namespaced attributes (e.g. `xsi:`) refused;
schema loaded locally and SHA-256 verified; no decompression.

**Not checked** (schema success does not establish): UID uniqueness, extent ordering/overlap/coverage, payload
checksums, tape layout, recovery, interoperability. `fixtures`/tests include a schema-valid semantic counterexample.
Limitations: the DOCTYPE pre-scan is a byte match (also rejects the string inside comments/CDATA); a non-`ltfsindex`
root is UNSUPPORTED, but incremental-index fragments of an `ltfsindex` root are only rejected by the schema (INVALID).
Depth beyond libxml2's own limit is reported as `XML_MALFORMED`.

## Web viewer (`web/`)

A static page for looking inside an index rather than just grading it: open `web/index.html` (straight from disk
works) and drop one or more index XML files on it. Everything runs in the tab; the page's Content-Security-Policy
has `connect-src 'none'`, so it cannot send the file anywhere. It shows:

- **Summary**: volume UUID, generation, update time, location and back pointer, entry and byte counts.
- **Tree**: lazily expanded directories (500 entries per page), name filter, symlink targets, per-entry badges.
- **Entry detail**: times, xattrs, extents table and a file-offset coverage bar (gaps = sparse, red = past `length`).
- **Generations**: several indexes of one volume are ordered by generation, the back-pointer chain is checked
  against loaded locations, and each generation is diffed against the previous one (added/removed/changed).
- **Findings**, three families:
  - `STRUCT_*` is a hand-written mirror of the Annex B rules. It is **not** XSD validation; `validate.py` stays the
    authority. The Playwright suite checks that the two agree on every case it covers (when `LTFS_PYTHON` is set).
    Annex B as printed declares `xattr/value` empty, so text values get `STRUCT_XATTR_VALUE_CONTENT`, matching the CLI.
  - `SEM_*` covers what the schema cannot: duplicate or out-of-range `fileuid` (`highestfileuid` 0 is the
    exhausted-UID sentinel, not a bound), duplicate/invalid names, extent
    order, overlap, past-`length` and sparse coverage, and cross-file tape overlap (uses the block-size box, default
    524288, because the block size lives in the label, not the index).
  - `GEN_*`: mixed volumes, broken back pointers, same-generation copies vs. conflicts, time/UID regressions.

Same refusals as the CLI (DOCTYPE/entities, non-UTF-8, namespaces, other versions, depth > 64); size cap 64 MiB.
Index values reach the page only through `textContent`. Not checked: payload checksums, tape layout, recovery.

    cd web && npm ci && npx playwright install chromium
    LTFS_PYTHON=../.venv/bin/python npx playwright test   # PW_CHROMIUM_PATH=... to use a preinstalled Chromium

Test indexes are synthetic, built by `web/tests/synth.js`, plus the repo `fixtures/`.
