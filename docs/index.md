# goblin-workshop

The crew's public workshop for small open tools: lightweight utilities that anyone
is welcome to use, fork, or improve. Each tool lives in its own directory of the
[repository](https://github.com/crew-exchange-2079/goblin-workshop) and is documented
by its README; the pages on this site are built from those READMEs.

## Tape tools

Open and vendor-agnostic, aimed at tape administrators. They are designed around
public standards (T10 SCSI log pages and MAM attributes via sg3_utils, LTFS, and plain
byte streams) and are developed from public specifications and synthetic data only.
That describes design intent, not testing: each tool's page states what it has and
has not been tested against.

| Tool | What it is |
| --- | --- |
| [tape-exporter](tools/tape-exporter.md) | Read-only Prometheus exporter for Linux LTO/SCSI tape drives. |
| [tapefec](tools/tapefec.md) | Streaming Reed–Solomon forward-error-correction framer for tar-to-tape pipelines; pure-Python educational prototype. |
| [tape-library-playground](tools/tape-library-playground.md) | Browser-based teaching simulator of a robotic tape library and its scheduling policies; not an operational controller. |
| [ltfs-index-validator](tools/ltfs-index-validator.md) | Offline schema-only checker for LTFS 2.4.0 full index XML; not compliance or certification. |
| [mhvtl-status](tools/mhvtl-status.md) | Regression kit for mhvtl's optional log pages, run against a fresh upstream checkout. |

## Other tools

| Tool | What it is |
| --- | --- |
| [rack-parts](tools/rack-parts.md) | Parametric OpenSCAD generator for printable rack parts (brackets, shelves, panels, PDU mounts). |

## Licensing

Licensing is per tool and stated on each tool's page.
