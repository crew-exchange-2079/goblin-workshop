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

Hardening: <=4 MiB read from a nonblocking no-follow regular file (no URLs, FIFOs, symlinks, directories, `/dev`);
UTF-8 only; depth<=64, nodes<=100000; DOCTYPE/entities refused; parser with no recovery, no huge tree, no DTD,
no default attributes, no entity resolution, deny-all resolver; namespaced attributes (e.g. `xsi:`) refused;
schema loaded locally and SHA-256 verified; no decompression.

**Not checked** (schema success does not establish): UID uniqueness, extent ordering/overlap/coverage, payload
checksums, tape layout, recovery, interoperability. `fixtures`/tests include a schema-valid semantic counterexample.
Limitations: the DOCTYPE pre-scan is a byte match (also rejects the string inside comments/CDATA); a non-`ltfsindex`
root is UNSUPPORTED, but incremental-index fragments of an `ltfsindex` root are only rejected by the schema (INVALID).
Depth beyond libxml2's own limit is reported as `XML_MALFORMED`.
