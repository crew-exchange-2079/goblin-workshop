# sg3_utils 1.46 golden output

Produced by the real `sg_logs` binary from Ubuntu 24.04 `sg3-utils
1.46-3ubuntu4.1` ("Version string: 1.81 20200110") decoding **synthetic**
log-page bytes offline, with no device involved:

```
sg_logs --in=write_page.hex  > write_page.txt
sg_logs --in=read_page.hex   > read_page.txt
sg_logs --in=verify_page.hex > verify_page.txt
```

SSC Sequential-Access Device page (0x0c), decoded as a tape drive
(`--pdt=1`; on a real device sg_logs learns the type from INQUIRY):

```
sg_logs --pdt=1 --verbose --in=seqaccess_page.hex       > seqaccess_page.txt
sg_logs --pdt=1 --verbose --in=seqaccess_clean_page.hex > seqaccess_clean_page.txt
sg_logs --pdt=1 --in=seqaccess_page.hex                 > seqaccess_page_nonverbose.txt
sg_logs --verbose --in=seqaccess_page.hex               > seqaccess_page_as_disk.txt
```

`seqaccess_page.hex` has the four byte counters (one above 2 TiB), a
capacity parameter, cleaning required (0x100 = 1) and a vendor parameter
(0x8000). `seqaccess_clean_page.hex` is a partial page with cleaning not
required. The nonverbose output shows why the exporter passes `--verbose`
for this page: without it only truncated decimal GB are printed. The
`as_disk` output shows that page 0x0c means Logical Block Provisioning on a
non-tape device; the parser rejects it (only the SSC heading is accepted).
Note that 1.46 spells the heading and labels differently from sg3_utils git
("page" vs "log page", "commands" vs "operations"); the parser accepts both
literally.

The `.hex` files are hand-made SPC/SSC log pages (page 0x02, 0x03, 0x05,
0x0c). They are not captures from any drive. `--in` and `--pdt` are used
only here, to make fixtures; the exporter never passes them (`--in` is a
forbidden token and `commands._check()` allows only two exact argv shapes).
