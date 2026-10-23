"""Synthetic lto-exporter scrape targets for the Grafana smoke test.

Runs INSIDE a disposable container on an internal Docker network. It serves
the real exporter's HTTP handler and Collector (so metric names, labels and
exposition format are exactly what lto-exporter emits), but the collector's
command runner is replaced by a fake that returns synthetic sg_logs text.
Nothing is executed and no device is opened: device checks are stubbed out.

  :9101  "healthy"  drive0 (all pages, counters grow every second,
                           cleaning not required)
                    drive1 (write page ok; read page missing parameters;
                           0x0c page: write/host bytes only, cleaning required)
  :9102  "failing"  drive2 (write page unsupported, read page timeout,
                           0x0c page not ready)
                    drive3 (pinned node missing: nothing is run)

Usage (in the container): python3 -I /smoke/synthetic_target.py /src
"""

import sys
import threading
import time

WRITE_HDR = "Write error counter log page  [0x2]"
READ_HDR = "Read error counter log page  [0x3]"
NAMES = (
    ("corrected_without_delay", "Errors corrected without substantial delay"),
    ("corrected_with_delay", "Errors corrected with possible delays"),
    ("rewrites_or_rereads", "Total rewrites or rereads"),
    ("total_corrected", "Total errors corrected"),
    ("correction_algorithm_processed", "Total times correction algorithm processed"),
    ("bytes_processed", "Total bytes processed"),
    ("total_uncorrected", "Total uncorrected errors"),
)
SA_HDR = "Sequential access device page (ssc-3)"   # sg3_utils 1.46 spelling
SA_NAMES = (
    ("bytes_received_write", "Data bytes received with WRITE commands"),
    ("bytes_written_media", "Data bytes written to media by WRITE commands"),
    ("bytes_read_media", "Data bytes read from media by READ commands"),
    ("bytes_transferred_read", "Data bytes transferred by READ commands"),
)
START = time.monotonic()


def page(header, values):
    """sg_logs 1.47-style text for an error counter page."""
    lines = ["    SYNTHVND  LTO-SIM           0001", header]
    lines += [f"  {text} = {values[key]}" for key, text in NAMES if key in values]
    return "\n".join(lines) + "\n"


def growing(base, per_second):
    return int(base + per_second * (time.monotonic() - START))


def healthy_page(device, direction):
    if device == "drive0":
        return page(WRITE_HDR if direction == "write" else READ_HDR, {
            "corrected_without_delay": growing(10, 1),
            "corrected_with_delay": growing(3, 0.5),
            "rewrites_or_rereads": growing(17, 2),
            "total_corrected": growing(13, 1.5),
            "correction_algorithm_processed": growing(21, 1),
            "bytes_processed": growing(2199023255553, 50_000_000),
            "total_uncorrected": growing(1, 0.2),
        })
    if direction == "write":
        return page(WRITE_HDR, {
            "corrected_without_delay": 0, "corrected_with_delay": 0,
            "rewrites_or_rereads": 0, "total_corrected": 0,
            "correction_algorithm_processed": 0,
            "bytes_processed": growing(1073741825, 1_000_000),
            "total_uncorrected": 0,
        })
    # drive1 read: page read but only two parameters reported
    return page(READ_HDR, {"corrected_without_delay": 5,
                           "bytes_processed": growing(1073741825, 1_000_000)})


def sa_page(values, cleaning):
    """sg_logs 1.46 --verbose text for the Sequential-Access Device page."""
    lines = ["    SYNTHVND  LTO-SIM           0001", SA_HDR]
    lines += [f"  {text}: {values[key] // 1000000000} GB [{values[key]} bytes]"
              for key, text in SA_NAMES if key in values]
    lines.append("  Cleaning action required" if cleaning
                 else "  Cleaning action not required (or completed)")
    lines.append(f"    cleaning value: {int(cleaning)}")
    return "\n".join(lines) + "\n"


def healthy_sa_page(device):
    if device == "drive0":
        return sa_page({
            "bytes_received_write": growing(2199023255553, 80_000_000),
            "bytes_written_media": growing(1099511627776, 40_000_000),
            "bytes_read_media": growing(549755813888, 20_000_000),
            "bytes_transferred_read": growing(1099511627776, 40_000_000),
        }, cleaning=False)
    return sa_page({"bytes_received_write": growing(1073741825, 1_000_000)}, cleaning=True)


def make_runner(RunResult, scenario):
    def runner(argv, timeout, max_bytes):
        direction = "write" if "--page=0x2" in argv else "read"
        device = {"/dev/sg10": "drive0", "/dev/sg11": "drive1",
                  "/dev/sg12": "drive2"}.get(argv[-1])
        if "--page=0xc" in argv:
            if scenario == "healthy":
                return RunResult("ok", 0, healthy_sa_page(device), 0.05)
            return RunResult("ok", 2, "", 0.01)   # sg_logs exit 2: not ready
        if scenario == "healthy":
            return RunResult("ok", 0, healthy_page(device, direction), 0.12)
        if direction == "write":
            return RunResult("ok", 5, "", 0.01)   # sg_logs exit 5: unsupported
        return RunResult("timeout", None, "", 10.0)
    return runner


def serve(src, scenario, port, devices, missing=()):
    sys.path.insert(0, src)
    from lto_exporter.commands import RunResult
    from lto_exporter.config import parse_config
    from lto_exporter.exporter import Collector, make_server

    config = parse_config({"listen_address": "0.0.0.0", "port": port,
                           "cache_seconds": 0, "devices": devices},
                          resolver=lambda p: p)
    collector = Collector(config, runner=make_runner(RunResult, scenario),
                          device_check=lambda d: "device_missing" if d.name in missing else None)
    server = make_server(config, collector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv):
    src = argv[1] if len(argv) > 1 else "/src"
    serve(src, "healthy", 9101, [{"name": "drive0", "path": "/dev/sg10"},
                                 {"name": "drive1", "path": "/dev/sg11"}])
    serve(src, "failing", 9102, [{"name": "drive2", "path": "/dev/sg12"},
                                 {"name": "drive3", "path": "/dev/sg13"}],
          missing=("drive3",))
    print("synthetic targets on :9101 (healthy) and :9102 (failing)", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main(sys.argv)
