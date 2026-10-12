# sg3_utils 1.46 golden output

Produced by the real `sg_logs` binary from Ubuntu 24.04 `sg3-utils
1.46-3ubuntu4.1` ("Version string: 1.81 20200110") decoding **synthetic**
log-page bytes offline, with no device involved:

```
sg_logs --in=write_page.hex  > write_page.txt
sg_logs --in=read_page.hex   > read_page.txt
sg_logs --in=verify_page.hex > verify_page.txt
```

The `.hex` files are hand-made SPC log pages (page 0x02, 0x03, 0x05). They
are not captures from any drive. `--in` is used only here, to make fixtures;
the exporter never passes it (it is a forbidden token in `commands.py`).
