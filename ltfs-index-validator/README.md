# ltfs-index-validator

Offline checker for LTFS 2.4.0 *full* index XML (unnamespaced, `version="2.4.0"`). It runs in two
layers:

1. **Schema**: the SNIA Annex B XSD, with one erratum fix by default (see below, `NOTICE`, `PROVENANCE.json`).
2. **Semantics**: UID uniqueness, extent overlap and bounds, self pointer (selflink) and back pointer
   (backptr) consistency, generation chains, UTF-8 and percent-encoding name rules, xattr encoding,
   and calendar-valid timestamps. Every rule and its source is listed in [`RULES.md`](RULES.md).

`VALID` is not LTFS compliance or certification. Payload checksums, tape contents and the volume
label are never read.

    python3 -m venv .venv
    .venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.txt
    .venv/bin/python validate.py --input fixtures/valid-nested.xml

## Usage

    validate.py --input INDEX.xml
                [--previous PREV.xml]                       # generation/back-pointer chain checks
                [--index-partition a --data-partition b]    # partition roles from the volume label
                [--blocksize 524288]                        # label block size: tape byte-range checks
                [--strict]                                  # warnings also make the index INVALID
                [--schema-only]                             # v0.1 behaviour: schema layer only
                [--schema errata1|annex-b]                  # default errata1

`--previous` must be the index that `--input`'s `previousgenerationlocation` points to. The two
partition options are given together and must differ. `--schema-only` cannot be combined with
`--strict`, `--previous`, `--blocksize` or the partition options.

## Exit codes and statuses

The tool writes one JSON report (≤ 64 KiB) to stdout. Its `status` and the process exit code always
agree:

| Exit | `status` | Meaning |
|---:|---|---|
| 0 | `VALID` | Schema-valid and no semantic **errors**. There may be warnings (`counts.warnings`); `--strict` turns them into exit 1. |
| 0 | `SCHEMA_VALID_ONLY` | Only with `--schema-only`: schema-valid, semantics not checked. |
| 1 | `INVALID` | Schema violation (`semantic_checks: "SKIPPED_SCHEMA_INVALID"`), or at least one semantic error (or warning under `--strict`). Also a DTD or entity, malformed XML, an invalid version string, or a depth/node limit. |
| 2 | `UNSUPPORTED` | Well-formed, but not something this tool validates: another LTFS version, a namespace, namespaced attributes, a root other than `ltfsindex`, or a non-UTF-8 encoding. |
| 3 | `ERROR` | The tool could not reach a verdict: a bad input path or kind, input too large, missing or tampered schema, missing dependency, a usage error, an unusable `--previous` (`PREVIOUS_INDEX_REJECTED`, `PREVIOUS_<read code>`), or an internal error. |

Any exit code other than 0–3 (for example a Python crash before the report is written) means no
verdict. Treat it like 3.

Report fields: `status`, `exit_code`, `input`/`previous` (SHA-256 and byte count), `schema` (variant,
actual and pinned SHA-256), `options`, `semantic_checks` (`PERFORMED`, `SKIPPED_SCHEMA_INVALID`,
`NOT_PERFORMED`), `counts` (`errors` and `warnings`, counting every diagnostic), and `diagnostics`.
Each diagnostic has a fixed `code`, a `severity`, a `line`, and a sanitised `path`. The first 50 are
kept, and `diagnostics_truncated` says whether any were dropped. Rejected XML values, names,
filenames and raw parser errors are never echoed.

## Schema erratum (`errata1`)

Annex B, as printed, declares xattr `<value>` as a complex type with only a `type` attribute: an
*empty* content model. Under that schema, any xattr that actually has a value is INVALID, and that
covers every index real LTFS implementations write. `schema/ltfs-index-2.4.0-errata1.xsd` makes one
change: `<value>` gets simple `xs:string` content plus the same `type` attribute. Both files are
SHA-256 pinned. `--schema annex-b` validates against the verbatim Annex B text.

## Tests

    .venv/bin/pip install --require-hashes --only-binary=:all: -r requirements-dev.txt   # hypothesis
    .venv/bin/python -m unittest discover -s tests -v

- `tests/test_validate.py`: input handling, hardening, CLI contract (v0.1 suite, extended).
- `tests/test_semantic.py`: each rule at its boundary, plus oracle properties (calendar against
  Python's `calendar`, percent decoding against `urllib.parse`).
- `tests/test_properties.py`: hypothesis. Generated indexes are `VALID` with zero diagnostics. Each
  mutation in `tests/indexgen.py` raises its own code. The verdict does not change when `xs:all`
  children are reordered. Arbitrary bytes and corrupted XML never crash or exceed the report bound.
  Names and values never appear in the report. Chain mutations are detected, and an invalid
  `--previous` gives `ERROR`. The `ci` profile is the default and is derandomised; set
  `HYPOTHESIS_PROFILE=explore` to run 2000 random examples per property.
- `tests/test_corpus.py`: runs every file in `corpus/` (6 good, 31 bad, one per diagnostic code)
  through the real CLI against `corpus/manifest.json`. It also checks that `tests/make_corpus.py`
  still reproduces the corpus byte for byte (regenerate it with `python tests/make_corpus.py`).

## Hardening (unchanged from v0.1)

Platform: Linux only (needs `O_PATH` and `/proc`); elsewhere every input is refused as `ERROR`. Input
is pinned with an `O_PATH|O_NOFOLLOW` descriptor (no device driver is opened) and checked as a
regular file. It is then reopened via `/proc/self/fd/N` with its inode identity verified. Paths that
resolve into `/dev` are refused.

Limits: at most 4 MiB read from a nonblocking, no-follow regular file (no URLs, FIFOs, symlinks,
directories or `/dev`). UTF-8 only; depth ≤ 64; nodes ≤ 100000; DOCTYPE and entities refused. The
parser runs with no recovery, no huge tree, no DTD, no default attributes, no entity resolution and
a deny-all resolver. Namespaced attributes (e.g. `xsi:`) are refused. Schemas are loaded locally
and SHA-256 verified. No decompression. All semantic checks are linear or `n log n` in the node
count.

Limitations: the DOCTYPE pre-scan is a byte match, so it also rejects the string inside comments
and CDATA. A non-`ltfsindex` root is UNSUPPORTED, but incremental-index fragments under an
`ltfsindex` root are only rejected by the schema (INVALID). Depth beyond libxml2's own limit is
reported as `XML_MALFORMED`.
