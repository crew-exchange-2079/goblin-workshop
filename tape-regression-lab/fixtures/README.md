# sg_logs fixture corpus

A public corpus of `sg_logs` text output for the SSC log pages a tape tool is
likely to meet, in **sg3_utils 1.46** wording (Ubuntu 24.04's package) and the
**newer** wording of sg3_utils 1.47 and later (1.49 here), plus a test that
every parser in [`tape-exporter`](../../tape-exporter/), including the
offline `tapeinfo` text parser, handles every fixture.

Everything here is synthetic. No page was captured from a drive, and no
device was opened to make them.

## How the fixtures are made

The text is produced by the **real** `sg_logs` binary, not typed by hand.
[`generate.py`](generate.py) builds each log page as bytes from a small spec
(SPC log page header, then parameter code, control byte, length, value),
writes it as ASCII hex to `pages/<id>.hex`, and runs each `sg_logs` offline
over that file:

```
sg_logs --pdt=1 [--vendor=lto5] [-v] --in=pages/<id>.hex > sg3_utils-<ver>/<id>[.v].txt
```

`--in` decodes a page from a file instead of a device. `--pdt=1` says
"sequential-access device", so sg_logs picks its SSC decoders as it would
after a tape drive's INQUIRY (without it, 0x0c decodes as the SBC *Logical
block provisioning* page). `--vendor=lto5` selects the LTO decoder for the
vendor-specific page 0x31. Each page is decoded twice, plain and with `-v`;
only `-v` prints exact byte counts on page 0x0c.

| `sg3_utils-` | binary | `sg_logs -V` |
|---|---|---|
| `1.46` | Ubuntu 24.04 package `sg3-utils 1.46-3ubuntu4.1` | `1.81 20200110` |
| `1.49` | upstream tag `release-1.49` (`b1f5e2ee…`), built from source | `2.37 20231026` |

sg3_utils 1.48 (`2.35 20230622`, Debian 13) was also run: its output is
byte-identical to 1.49 for every fixture, so it is not stored twice.

`manifest.json` lists every fixture: page and subpage, description,
`read_may_clear`, the hex file, and for every output its exact argv, exit
status, stderr, sha256 and the result each tape-exporter parser must give.
Those expectations are computed from the page spec in `generate.py`, never
by running the parsers.

## Pages

| page | fixtures | notes |
|---|---|---|
| 0x02 Write error counters | typical, all zero, uint64 edge, partial + vendor param | exporter reads this page |
| 0x03 Read error counters | typical, all zero, partial + vendor params | exporter reads this page |
| 0x0c Sequential access device | cleaning required / not, all zero, partial | exporter reads this page (`--verbose`) |
| 0x2e TapeAlert | all clear; flags 3, 4, 0x14, 0x1e set | **read-may-clear**, see below |
| 0x31 Tape capacity (LTO-5/6) | main/alternate partition remaining and maximum | vendor-specific (`--vendor=lto5`) |
| 0x17 Volume statistics, subpage 0 | page valid, counts, volume serial | SSC-4 |

**0x2e is read-may-clear.** Under SSC, TapeAlert flags can be reset by
reading the page (whether they are depends on the drive and on the
Informational Exceptions mode page). A monitor that reads it can hide an alert
from the backup software or library that was meant to act on it. tape-exporter
never reads 0x2e (`commands.py` only allows `--page=0x2`, `0x3` and `0xc`);
these fixtures exist so every parser is shown to reject TapeAlert text, and
they are flagged `"read_may_clear": true` in the manifest. 0x31 and 0x17 are
here for the same reason: no parser reads them, and each must say
`parse_error` rather than pick numbers out of them.

Wording differences the corpus pins down (1.46 → 1.47+): page headers gain
`log` (`Write error counter page` → `Write error counter log page`,
`Tape alert page` → `TapeAlert log page`), output starts with a blank line,
0x0c says `operations` for `commands` and `Cleaning action required (or in
progress)` for `Cleaning action required`, error counters of 1 GB or more
gain a `[N GB]` suffix, and TapeAlert flags 0x2f-0x31 and 0x3d are named.

## The test

```sh
python -m pytest -q test_corpus.py      # from this directory; needs pytest only
```

[`test_corpus.py`](test_corpus.py) imports the parsers from this
checkout and, for every output file and every parser, checks the parser's
status and values against the manifest. It also checks that the sha256 values
match, that no file is unlisted, that each fixture has both 1.46 and newer
output, that only 0x2e is read-may-clear, that every prefix of every fixture
parses without raising, and that a prefix cut at a line boundary reports only
values of the full page. The offline `tapeinfo` parser must extract nothing
from any fixture or line-boundary prefix. A new `parse_*` function in
`lto_exporter/parsers.py` or `offline/tapeinfo_text.py` fails the test until
it is added to the corpus.

A prefix cut inside a number can make a parser report a shorter number
(`= 21990` for `= 2199023255553`). The exporter never parses such output,
because it requires sg_logs to exit 0 after writing everything.

## Regenerating or checking

```sh
sudo apt-get install sg3-utils                       # 1.46 on Ubuntu 24.04
git clone --depth 1 --branch release-1.49 https://github.com/doug-gilbert/sg3_utils
(cd sg3_utils && ./autogen.sh && ./configure --disable-shared \
   && make -C lib && make -C src sg_logs)
python3 generate.py --check --sg-logs 1.46=/usr/bin/sg_logs \
                            --sg-logs 1.49=sg3_utils/src/sg_logs
```

Drop `--check` to rewrite the corpus after editing the spec in
`generate.py`. CI runs `--check` against both binaries, so a fixture
that the real decoder would not produce fails the build.

The fixtures are output of sg3_utils run on our own synthetic pages; no
sg3_utils source is included.
