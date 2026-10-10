"""Fixed, read-only command construction and a bounded subprocess runner.

No shell is used. Argument vectors are built only from constants plus the
validated device path. Options that would modify device state (sg_logs
--reset/--select, any mt/sg_* motion or write tool) are never constructed.
"""

import os
import selectors
import subprocess
import time
from dataclasses import dataclass

# sg_logs(8): --page=PG selects the log page; --readonly opens the device
# O_RDONLY (default is to try read-write first). Page 0x2 = write error
# counters, 0x3 = read error counters.
SG_LOGS_PAGES = {"write": "0x2", "read": "0x3"}

FORBIDDEN_TOKENS = frozenset({
    "--reset", "-R", "-r", "--select", "-S", "--sp", "-s", "--control",
    "--in", "-i",
})

SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


class UnsafeCommand(ValueError):
    pass


def _check(argv):
    if os.path.basename(argv[0]) != "sg_logs":
        raise UnsafeCommand(f"only sg_logs may be executed, got {argv[0]!r}")
    if any(tok in FORBIDDEN_TOKENS for tok in argv[1:-1]):
        raise UnsafeCommand(f"refusing to build unsafe argv: {argv!r}")
    return argv


def sg_logs_argv(exe, direction, device_path):
    page = SG_LOGS_PAGES[direction]
    return _check([exe, "--readonly", f"--page={page}", device_path])


@dataclass
class RunResult:
    status: str  # ok | timeout | not_found | exec_error | output_too_large
    returncode: int | None
    stdout: str
    duration: float


def run_command(argv, timeout, max_bytes, popen=subprocess.Popen, clock=time.monotonic):
    start = clock()
    try:
        proc = popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=SAFE_ENV, shell=False, close_fds=True,
        )
    except FileNotFoundError:
        return RunResult("not_found", None, "", clock() - start)
    except OSError:
        return RunResult("exec_error", None, "", clock() - start)

    buf = bytearray()
    status = "ok"
    deadline = start + timeout
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)
    try:
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                status = "timeout"
                break
            if not sel.select(remaining):
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > max_bytes:
                status = "output_too_large"
                break
    finally:
        sel.close()
        if status != "ok":
            proc.kill()
        try:
            proc.wait(timeout=max(0.1, deadline - clock()) if status == "ok" else 2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=2)
            if status == "ok":
                status = "timeout"
        proc.stdout.close()
    text = bytes(buf[:max_bytes]).decode("utf-8", errors="replace")
    rc = proc.returncode if status == "ok" else None
    return RunResult(status, rc, text, clock() - start)
