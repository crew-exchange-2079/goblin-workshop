"""Collection, Prometheus text rendering and the /metrics HTTP endpoint."""

import argparse
import functools
import signal
import socket
import sys
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from lto_exporter import __version__
from lto_exporter.commands import ChildTracker, run_command, sg_logs_argv
from lto_exporter.config import (ConfigError, check_device,
                                 check_sg_logs_executable, load_config,
                                 validate_listen_address, validate_port)
from lto_exporter.parsers import (PARAM_LABELS, SA_FIELDS,
                                  parse_sg_logs_error_counters,
                                  parse_sg_logs_sequential_access,
                                  sg_exit_status)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
DIRECTIONS = ("write", "read")
SA_COLLECTOR = "sg_logs_sequential_access"
COLLECTORS = tuple(f"sg_logs_{d}_errors" for d in DIRECTIONS) + (SA_COLLECTOR,)
# Sequential-Access Device page byte counters -> (direction, side).
# "host" = bytes moved over the SCSI bus (before compression), "media" =
# bytes written to / read from tape (after compression).
SA_BYTES = {
    "bytes_received_write": ("write", "host"),
    "bytes_written_media": ("write", "media"),
    "bytes_read_media": ("read", "media"),
    "bytes_transferred_read": ("read", "host"),
}
SIDES = ("host", "media")

# Every value the `status` label can take. See README "Collector status".
COLLECTOR_STATUSES = (
    "ok",
    # sg_logs exit status (parsers.SG_EXIT_STATUS)
    "syntax_error", "not_ready", "unit_attention", "unsupported", "file_error", "device_timeout",
    "os_error", "error",
    # output parsing
    "parse_error",
    # runner (commands.RunResult)
    "timeout", "output_too_large", "read_error", "not_found", "exec_error",
    "blocked_unreaped",
    # per-scrape device re-check (config.check_device)
    "device_changed", "device_mismatch", "device_missing",
    # unexpected exception inside the exporter
    "internal_error",
)

# Label names per metric family, excluding target labels (instance, job).
METRIC_LABELS = {
    "tape_exporter_build_info": ("version",),
    "tape_exporter_unreaped_children": (),
    "tape_exporter_collector_success": ("device", "collector"),
    "tape_exporter_collector_duration_seconds": ("device", "collector"),
    "tape_exporter_collector_status": ("device", "collector", "status"),
    "tape_log_parameter_present": ("device", "direction", "parameter"),
    "tape_log_errors_total": ("device", "direction", "parameter"),
    "tape_bytes_processed_total": ("device", "direction"),
    "tape_sequential_access_field_present": ("device", "field"),
    "tape_data_bytes_total": ("device", "direction", "side"),
    "tape_cleaning_required": ("device",),
}

HELP = {
    "tape_exporter_build_info": ("gauge", "Exporter version."),
    "tape_exporter_unreaped_children": ("gauge", "sg_logs children killed after a timeout but not yet reaped (e.g. stuck in D state). New commands for their device are not spawned while this is non-zero."),
    "tape_exporter_collector_success": ("gauge", "1 if the collector ran and its output parsed."),
    "tape_exporter_collector_duration_seconds": ("gauge", "Wall time of the collector command."),
    "tape_exporter_collector_status": ("gauge", "1 for the collector's current result class."),
    "tape_log_parameter_present": ("gauge", "1 if the drive returned this error-counter parameter, 0 if absent/unparsable."),
    "tape_log_errors_total": ("counter", "SCSI Write/Read Error Counter log page parameter, as reported by the drive (resets with the drive)."),
    "tape_bytes_processed_total": ("counter", "Error Counter log page 'Total bytes processed', as reported by the drive."),
    "tape_sequential_access_field_present": ("gauge", "1 if the Sequential-Access Device log page reported this field with an exact value, 0 if absent/unparsable/conflicting."),
    "tape_data_bytes_total": ("counter", "Sequential-Access Device log page byte counter: side=host is data moved over SCSI, side=media is data written to/read from tape (resets with the drive)."),
    "tape_cleaning_required": ("gauge", "1 if the drive reports cleaning required (or in progress), 0 if not required (or completed). Absent if unknown."),
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
    def __init__(self, config, runner=None, clock=time.monotonic,
                 tracker=None, device_check=check_device):
        self.config = config
        self.tracker = tracker if tracker is not None else ChildTracker()
        self.runner = runner or functools.partial(run_command, tracker=self.tracker)
        self.clock = clock
        self.device_check = device_check
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

    def _one_page(self, dev, page_name, parse):
        """Return (status, duration, page-or-None). Never raises."""
        try:
            argv = sg_logs_argv(self.config.sg_logs_path, page_name, dev.resolved)
            res = self._run(argv)
        except Exception:  # never let one command break the whole scrape
            return "internal_error", 0.0, None
        status = res.status
        if status == "ok":
            status = sg_exit_status(res.returncode)
        page = None
        if status == "ok":
            try:
                page = parse(res.stdout)
            except Exception:
                return "internal_error", res.duration, None
            status = page.status
        return status, res.duration, page

    def _sg_logs(self, s, dev, bad):
        for direction in DIRECTIONS:
            name = f"sg_logs_{direction}_errors"
            if bad:
                self._record(s, dev, name, bad, 0.0)
                continue
            status, duration, page = self._one_page(
                dev, direction,
                functools.partial(parse_sg_logs_error_counters, expected_direction=direction))
            self._record(s, dev, name, status, duration)
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
                    s.add("tape_log_errors_total",
                          {"device": dev.name, "direction": direction, "parameter": label},
                          page.values[label])

    def _sequential_access(self, s, dev, bad):
        if bad:
            self._record(s, dev, SA_COLLECTOR, bad, 0.0)
            return
        status, duration, page = self._one_page(
            dev, "sequential_access", parse_sg_logs_sequential_access)
        self._record(s, dev, SA_COLLECTOR, status, duration)
        if status != "ok":
            return
        for field in SA_FIELDS:
            present = field in page.values
            s.add("tape_sequential_access_field_present",
                  {"device": dev.name, "field": field}, 1 if present else 0)
            if not present:
                continue
            if field == "cleaning_action_required":
                s.add("tape_cleaning_required", {"device": dev.name}, page.values[field])
            else:
                direction, side = SA_BYTES[field]
                s.add("tape_data_bytes_total",
                      {"device": dev.name, "direction": direction, "side": side},
                      page.values[field])

    def collect(self):
        with self._lock:
            now = self.clock()
            if (self._cached is not None
                    and now - self._cached_at < self.config.cache_seconds):
                return self._cached
            s = Samples()
            s.add("tape_exporter_build_info", {"version": __version__}, 1)
            for dev in self.config.devices:
                try:
                    bad = self.device_check(dev)
                except Exception:
                    bad = "device_mismatch"
                if "sg_logs" in dev.collectors:
                    self._sg_logs(s, dev, bad)
                if "sequential_access" in dev.collectors:
                    self._sequential_access(s, dev, bad)
            s.add("tape_exporter_unreaped_children", {}, self.tracker.count())
            self._cached = s.render()
            self._cached_at = self.clock()
            return self._cached


_CLIENT_GONE = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                socket.timeout)


def make_handler(collector):
    class Handler(BaseHTTPRequestHandler):
        server_version = "lto-exporter"
        sys_version = ""
        timeout = 30  # per-connection socket timeout (slow/idle clients)

        def _send(self, code, body, ctype="text/plain; charset=utf-8"):
            data = body.encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)
                    self.wfile.flush()
            except _CLIENT_GONE:
                self.close_connection = True

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


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Default prints a traceback to stderr. Client disconnects and the
        # like are not actionable; stay quiet and don't leak details.
        pass


class _Server6(_Server):
    address_family = socket.AF_INET6


def make_server(config, collector=None):
    collector = collector or Collector(config)
    cls = _Server6 if ":" in config.listen_address else _Server
    return cls((config.listen_address, config.port), make_handler(collector))


def _port_arg(text):
    try:
        return validate_port(int(text, 10))
    except (ValueError, ConfigError):
        raise argparse.ArgumentTypeError("port must be an integer between 0 and 65535") from None


def _addr_arg(text):
    try:
        return validate_listen_address(text)
    except ConfigError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def build_arg_parser():
    ap = argparse.ArgumentParser(
        prog="lto-exporter",
        description="Read-only LTO/SCSI tape Prometheus exporter")
    ap.add_argument("--config", required=True, help="path to JSON config")
    ap.add_argument("--listen-address", type=_addr_arg, help="override listen_address")
    ap.add_argument("--port", type=_port_arg, help="override port (0-65535)")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def main(argv=None, sg_logs_check=check_sg_logs_executable):
    args = build_arg_parser().parse_args(argv)
    try:
        cfg = load_config(args.config)
        sg_logs_check(cfg.sg_logs_path)
    except (OSError, ValueError, ConfigError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.listen_address is not None:
        cfg = replace(cfg, listen_address=args.listen_address)
    if args.port is not None:
        cfg = replace(cfg, port=args.port)
    try:
        server = make_server(cfg)
    except OSError as exc:
        print(f"cannot listen on {cfg.listen_address}:{cfg.port}: {exc.strerror}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    print(f"listening on http://{host}:{port}/metrics", file=sys.stderr)
    for dev in cfg.devices:
        print(f"device {dev.name}: {dev.path} pinned to {dev.resolved}", file=sys.stderr)
    # systemd stop / docker stop: exit 0 promptly. Do NOT call
    # server.shutdown() here: it blocks in the serve_forever loop's exit
    # wait and deadlocks the handler thread.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(0))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
