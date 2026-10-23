# Semantic rules (v0.2)

Semantic checks run only after the document passes the schema (`semantic_checks: "PERFORMED"`).
Each rule emits one fixed code with a line number and a sanitised element path. Names, values and
numbers from the input never appear in a report.

**Where the rules come from.** The rules are grounded in the reference implementation pinned in
`PROVENANCE.json` (`LinearTapeFileSystem/ltfs@efc9e3a`), which was read but never built or run. Each
rule cites the file and function it follows. The SNIA specification prose was not available when
v0.2 was written, so no rule cites a clause number. A rule marked *validator* goes beyond what the
reference reader enforces; those are explained in the "Why" column.

**Severity.** `error` makes the index `INVALID` (exit 1). `warning` is reported, but the index stays
`VALID` (exit 0) unless `--strict` is given.

## Index header and tape pointers

`location` is the index's self pointer (selflink). `previousgenerationlocation` is its back pointer
(backptr). A partition is laid out as VOL1(0) FM(1) label(2) FM(3), then FM(4) and the first index at
block 5 (`ltfs.c` `ltfs_write_label`, `ltfs_write_index`).

| Code | Sev | Rule | Basis |
|---|---|---|---|
| `LOCATION_IN_LABEL_AREA` | error | `location/startblock` ≥ 5 | label layout above; the reference checks the same bound for backptr |
| `BACKPTR_IN_LABEL_AREA` | error | `previousgenerationlocation/startblock` ≥ 5 | `ltfs_internal.c` `ltfs_read_index` (11197E) |
| `BACKPTR_NOT_BEFORE_SELF` | error | if backptr and selflink share a partition, backptr ≤ selflink − 3 (index, FM, FM, index) | `ltfs_read_index` (11197E). *validator*: the reference skips this when selflink is block 5; here a forward pointer is always an error |
| `BACKPTR_IS_SELF` | warning | backptr equals selflink | the reference tolerates it; it cannot name a previous generation |
| `BACKPTR_MISSING` | warning | no backptr although selflink > block 5 | `ltfs.c` `ltfs_write_index` always carries a backptr once one data-partition index exists |
| `PARTITION_COUNT_EXCEEDED` | error | at most 2 distinct partition letters across selflink, backptr and extents | an LTFS volume has exactly an index and a data partition |
| `LOCATION_PARTITION_UNKNOWN` | error | with `--index-partition/--data-partition`: selflink is one of them | `ltfs_read_index` (selflink vs label) |
| `BACKPTR_NOT_DATA_PARTITION` | error | with partition facts: backptr is in the data partition | `ltfs_read_index` (11197E) |
| `COMMENT_TOO_LONG` | error | `comment` ≤ 65536 UTF-8 bytes | `xml_reader_libltfs.c` (17094E), `ltfs.h` `INDEX_MAX_COMMENT_LEN` |
| `DATETIME_INVALID` | error | every `*time` is a real calendar instant (month 1–12, day within month incl. leap years, hour ≤ 23, minute ≤ 59, second ≤ 60) | the schema pattern accepts `2026-02-30T25:61:61`. *validator*: the reference only warns when a time does not fit in `time_t` |

## UIDs

| Code | Sev | Rule | Basis |
|---|---|---|---|
| `ROOT_UID_NOT_ONE` | error | root directory `fileuid` is 1 | `fs.c` `fs_allocate_dentry`; 17101E text "UID 1 is reserved for the root directory" |
| `UID_ZERO` | error | no `fileuid` is 0 | `xml_reader_libltfs.c` `_xml_parse_file` (17101E) |
| `UID_DUPLICATE` | error | `fileuid` values are unique across the index (LTFS has no hard links) | *validator*: the reference does not detect duplicates on read |
| `HIGHESTFILEUID_TOO_LOW` | error | `highestfileuid` ≥ every `fileuid` | the reference silently raises its counter (`uid_number`); a lower value would let a writer reuse a UID |

## Names (`name`, xattr `key`, `symlink`, placement-policy `name`)

When `percentencoded="true"`, each `%XX` is decoded to a byte and the result must be UTF-8. Then:

| Code | Sev | Rule | Basis |
|---|---|---|---|
| `NAME_PERCENT_ENCODING_MALFORMED` | error | every `%` starts a `%XX` escape with two hex digits | `decode_entry_name`. *validator*: the reference leaves a malformed tail undecoded |
| `NAME_NOT_UTF8` | error | the decoded bytes are strict UTF-8 (no overlongs, no surrogates) | `pathname.c` `_pathname_validate` (ICU UTF-8 check) |
| `NAME_FORBIDDEN_CHAR` | error | no U+0000, U+FFFE, U+FFFF; no `/` except in symlink targets | `pathname.c` `_pathname_valid_in_xml`, `_pathname_validate`. *validator*: the reference turns a decoded `/` into `_` or keeps `%2F` ("bad manner writer"); here it is an error |
| `NAME_EMPTY` | error | names, keys and targets are non-empty; the root directory's name (the volume name) may be empty | `_xml_parse_nametype` vs `_xml_parse_nametype_allow_zero_length` |
| `NAME_TOO_LONG` | error | ≤ 255 code points after NFC (not applied to symlink targets) | `ltfs.h` `LTFS_FILENAME_MAX`, `pathname_validate_file` |
| `NAME_DUPLICATE` | error | sibling names are unique after NFC | a path must resolve to one entry |
| `NAME_NOT_NFC` | warning | the name is already NFC | the reference normalises names to NFC (`pathname_format`) |
| `NAME_CASE_COLLISION` | warning | siblings differ by more than case | collide on case-insensitive hosts (`pathname_caseless_match`) |
| `NAME_DOT_ENTRY` | warning | a file or directory is not named `.` or `..` | unreachable through POSIX or Windows paths |

Deliberate divergence: U+001F is **accepted**. The reference rejects it, and its own source calls
that a historical bug to be removed (LinearTapeFileSystem/ltfs issue 106).

## Extended attributes

These checks need the `errata1` schema. Annex B as printed gives `<value>` an empty content model, so
the verbatim schema rejects any xattr that has a value (see `PROVENANCE.json`).

| Code | Sev | Rule | Basis |
|---|---|---|---|
| `XATTR_BASE64_INVALID` | error | a `type="base64"` value decodes as strict base64 (XML whitespace ignored) | `xml_reader_libltfs.c` `_xml_parse_one_xattr` (17028E) |
| `XATTR_KEY_DUPLICATE` | error | xattr keys on one entry are unique after NFC | an attribute name maps to one value |
| `XATTR_VALUE_LARGE` | warning | the decoded value is ≤ 4096 bytes | `ltfs.h` `LTFS_MAX_XATTR_SIZE`, an implementation limit, not a format rule |

The `type` attribute is restricted to `text` and `base64` by the schema itself (17027E).

## Extents

| Code | Sev | Rule | Basis |
|---|---|---|---|
| `EXTENT_OVERLAP` | error | the file-offset ranges `[fileoffset, fileoffset+bytecount)` of one file do not overlap | `_xml_parse_one_extent` (17097E) |
| `EXTENT_BEYOND_LENGTH` | error | `fileoffset + bytecount` ≤ `length` | `_xml_parse_file` (17026E) |
| `EXTENT_UNSORTED` | warning | extents are listed in ascending `fileoffset` | the reference re-sorts them, so this is legal but unusual |
| `EXTENT_IN_LABEL_AREA` | error | `startblock` ≥ 4 | blocks 0–3 are the label construct |
| `EXTENT_AFTER_INDEX` | error | an extent in the index's own partition starts before `location` | LTFS appends, so data an index describes was written before it |
| `EXTENT_COVERS_INDEX` | error | no extent covers the backptr block in its partition (start block only, or the whole span with `--blocksize`) | an index block cannot also be file data |
| `EXTENT_PARTITION_UNKNOWN` | error | with partition facts: extent partition is the index or data partition | `_xml_parse_partition` (17033E) |
| `EXTENT_BYTEOFFSET_EXCEEDS_BLOCK` | error | with `--blocksize`: `byteoffset` < block size | the offset is within the start block |
| `EXTENT_TAPE_SHARED` | warning | with `--blocksize`: no two extents occupy the same tape bytes | the reference never writes shared extents, and nothing in the format permits or forbids them |

Zero-length files, files with an empty `extentinfo`, and sparse files (gaps between extents, or
`length` past the last extent) are all valid.

## Generation chain (`--previous PATH`)

`--previous` names the index that `--input`'s `previousgenerationlocation` points to. That index must
itself be `VALID`. If it is not, or it cannot be read, the result is `ERROR` (exit 3) with
`PREVIOUS_INDEX_REJECTED` or `PREVIOUS_<read code>`, never `INVALID`.

| Code | Sev | Rule | Basis |
|---|---|---|---|
| `PREVIOUS_VOLUME_MISMATCH` | error | same `volumeuuid` (case-insensitive) | `ltfs_read_index` (11195W) |
| `PREVIOUS_NOT_REFERENCED` | error | `previousgenerationlocation` equals the previous index's `location` | definition of the back pointer |
| `GENERATION_REGRESSION` | error | `generationnumber` ≥ previous | generations only increase (`ltfs_write_index`) |
| `GENERATION_NOT_INCREASING` | error | strictly greater when both indexes are in the same partition. An index-partition copy may repeat the generation of the data-partition index its backptr names | `ltfs.c` `ltfs_write_index` moves backptr only from a data-partition selflink, so the index-partition copy of generation N points at data-partition generation N |
| `HIGHESTFILEUID_REGRESSION` | error | `highestfileuid` ≥ previous | UIDs are never reused |
| `UPDATETIME_REGRESSION` | warning | `updatetime` ≥ previous | clocks can step backwards, so a warning only |

## Not checked

Payload checksums and tape contents; the volume label and its block size (supply `--blocksize` and
the partition letters yourself); chains longer than one hop (validate each pair); `openforwrite`;
the `ltfs.*` xattr namespace; timestamps relative to each other; reachability of symlink targets;
data-placement-policy semantics.
