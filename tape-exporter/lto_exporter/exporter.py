"""Collection, Prometheus text rendering and the /metrics HTTP endpoint."""

import argparse
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lto_exporter import __version__
from lto_exporter.commands import run_command, sg_logs_argv
from lto_exporter.config import ConfigError, load_config
from lto_exporter.parsers import (PARAM_LABELS, SG_EXIT_STATUS,
                                  parse_sg_logs_error_counters)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

HELP = {
    "tape_exporter_build_info": ("gauge", "Exporter version."),
    "tape_exporter_collector_success": ("gauge", "1 if the collector ran and its output parsed."),
    "tape_exporter_collector_duration_seconds": ("gauge", "Wall time of the collector command."),
    "tape_exporter_collector_status": ("gauge", "1 for the collector's current result class."),
    "tape_log_parameter_present": ("gauge", "1 if the drive returned this error-counter parameter, 0 if absent/unparsable."),
    "tape_error_counter_total": ("counter", "SCSI Write/Read Error Counter log page parameter, as reported by the drive (resets with the drive)."),
    "tape_bytes_processed_total": ("counter", "Error Counter log page 'Total bytes processed', as reported by the drive."),
}

def _esc(v):
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Samples:
    def __init__(self):
        self.rows = {}

    def add(self, name, labels, value):
        self.rows.setdefault(name, []).append((labels, value))

    def render(self):
        out = []
        for name in HELP:
            rows = self.rows.get(name)
            if not rows:
                continue
            mtype, text = HELP[name]
            out.append(f"# HELP {name} {text}")
            out.append(f"# TYPE {name} {mtype}")
            for labels, value in rows:
                lbl = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
                out.append(f"{name}{{{lbl}}} {value}" if lbl else f"{name} {value}")
        return "\n".join(out) + "\n"


def _fmt(v):
    return repr(round(v, 6)) if isinstance(v, float) else str(v)


class Collector:
    def __init__(self, config, runner=run_command, clock=time.monotonic):
        self.config = config
        self.runner = runner
        self.clock = clock
        self._lock = threading.Lock()
        self._cached = None
        self._cached_at = None

    def _run(self, argv):
        c = self.config
        return self.runner(argv, c.command_timeout_seconds, c.max_output_bytes)

    def _record(self, s, dev, collector, status, duration):
        base = {"device": dev.name, "collector": collector}
        s.add("tape_exporter_collector_success", base, 1 if status == "ok" else 0)
        s.add("tape_exporter_collector_duration_seconds", base, _fmt(duration))
        s.add("tape_exporter_collector_status", {**base, "status": status}, 1)

    def _sg_logs(self, s, dev):
        for direction in ("write", "read"):
            name = f"sg_logs_{direction}_errors"
            res = self._run(sg_logs_argv(self.config.sg_logs_path, direction, dev.path))
            status = res.status
            page = None
            if status == "ok":
                status = SG_EXIT_STATUS.get(res.returncode, "error")
            if status == "ok":
                page = parse_sg_logs_error_counters(res.stdout, direction)
                status = page.status
            self._record(s, dev, name, status, res.duration)
            if status != "ok":
                continue
            for label in PARAM_LABELS:
                present = label in page.values
                s.add("tape_log_parameter_present",
                      {"device": dev.name, "direction": direction, "parameter": label},
                      1 if present else 0)
                if not present:
                    continue
                if label == "bytes_processed":
                    s.add("tape_bytes_processed_total",
                          {"device": dev.name, "direction": direction},
                          page.values[label])
                else:
                    s.add("tape_error_counter_total",
                          {"device": dev.name, "direction": direction, "parameter": label},
                          page.values[label])

    def collect(self):
        with self._lock:
            now = self.clock()
            if (self._cached is not None
                    and now - self._cached_at < self.config.cache_seconds):
                return self._cached
            s = Samples()
            s.add("tape_exporter_build_info", {"version": __version__}, 1)
            for dev in self.config.devices:
                if "sg_logs" in dev.collectors:
                    self._sg_logs(s, dev)
            self._cached = s.render()
            self._cached_at = self.clock()
            return self._cached


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lto-exporter"
        sys_version = ""

        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/metrics":
                try:
                    body = collector.collect()
                except Exception:  # never leak tracebacks/device output
                    self._send(500, "collection failed\n")
                    return
                self._send(200, body, CONTENT_TYPE)
            elif path == "/":
                self._send(200, "lto-exporter: see /metrics\n")
            else:
                self._send(404, "not found\n")

        do_HEAD = do_GET

        def log_message(self, fmt, *args):
            pass

    return Handler


def make_server(config, collector=None):
    collector = collector or Collector(config)
    return ThreadingHTTPServer((config.listen_address, config.port),
                               make_handler(collector))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only LTO/SCSI tape Prometheus exporter")
    ap.add_argument("--config", required=True, help="path to JSON config")
    ap.add_argument("--listen-address", help="override listen_address")
    ap.add_argument("--port", type=int, help="override port")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    from dataclasses import replace
    if args.listen_address:
        cfg = replace(cfg, listen_address=args.listen_address)
    if args.port is not None:
        cfg = replace(cfg, port=args.port)
    server = make_server(cfg)
    host, port = server.server_address[:2]
    print(f"listening on http://{host}:{port}/metrics", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
