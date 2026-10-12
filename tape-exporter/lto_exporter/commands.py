"""Fixed, read-only command construction and a bounded subprocess runner.

No shell is used. The only command this module will run is

    <sg_logs_path> --readonly --page=0x2|0x3 /dev/sgN

`_check()` enforces that exact shape on every argument, including the last
one, and `run_command()` calls it itself, so a caller that builds its own
argv can't bypass it. Options that would modify device state (sg_logs
--reset/--select, any mt/sg_* motion or write tool) are never constructed.

The runner is bounded: per-command timeout, output cap, and a bounded wait
for the child after SIGKILL. SIGKILL does not guarantee termination: a child
in uninterruptible sleep (D state, typically stuck in the kernel on a hung
device or HBA) can't be reaped until the kernel lets it go. Such children are
remembered in a ChildTracker, and no new command is spawned for the same
device (or at all, past a small global cap) until they have been reaped.
"""

import os
import re
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass

# sg_logs(8): --page=PG selects the log page; --readonly opens the device
# O_RDONLY (default is to try read-write first). Page 0x2 = write error
# counters, 0x3 = read error counters.
SG_LOGS_PAGES = {"write": "0x2", "read": "0x3"}
ALLOWED_PAGE_ARGS = frozenset(f"--page={p}" for p in SG_LOGS_PAGES.values())

# Defence in depth only; the exact-shape check below is what enforces safety.
# Long options are also refused in their --opt=value form.
FORBIDDEN_TOKENS = frozenset({
    "--reset", "-R", "--select", "-S", "--sp", "-s", "--control", "-c",
    "--in", "-i", "--raw", "-r", "--all", "-a", "--All", "-A",
})

SG_DEVICE_RE = re.compile(r"/dev/sg[0-9]{1,4}")
_CTRL_RE = re.compile(r"[\x00-\x20\x7f]")

SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}

# Bounded wait for a child after SIGKILL. Not a guarantee it is gone.
KILL_WAIT_SECONDS = 2.0
# Global cap on killed-but-unreaped children before all spawning stops.
MAX_UNREAPED = 4


class UnsafeCommand(ValueError):
    pass


def _check(argv):
    """Return argv as a list if it is exactly an allowed sg_logs command."""
    if (not isinstance(argv, (list, tuple)) or len(argv) != 4
            or not all(isinstance(t, str) for t in argv)):
        raise UnsafeCommand("argv must be exactly [sg_logs, --readonly, --page=0x2|0x3, /dev/sgN]")
    exe, readonly, page, device = argv
    if (not os.path.isabs(exe) or _CTRL_RE.search(exe)
            or os.path.basename(exe) != "sg_logs"):
        raise UnsafeCommand(f"only an absolute path to sg_logs may be executed, got {exe!r}")
    for tok in argv[1:]:
        if tok in FORBIDDEN_TOKENS or tok.split("=", 1)[0] in FORBIDDEN_TOKENS:
            raise UnsafeCommand(f"refusing forbidden option {tok!r}")
    if readonly != "--readonly":
        raise UnsafeCommand("second argument must be --readonly")
    if page not in ALLOWED_PAGE_ARGS:
        raise UnsafeCommand(f"page must be one of {sorted(ALLOWED_PAGE_ARGS)}")
    if not SG_DEVICE_RE.fullmatch(device):
        raise UnsafeCommand("last argument must be a /dev/sgN path")
    return list(argv)


def sg_logs_argv(exe, direction, device_path):
    page = SG_LOGS_PAGES[direction]
    return _check([exe, "--readonly", f"--page={page}", device_path])


@dataclass
class RunResult:
    # ok | timeout | output_too_large | read_error | not_found | exec_error
    # | blocked_unreaped
    status: str
    returncode: int | None
    stdout: str
    duration: float
    reaped: bool = True


class ChildTracker:
    """Children that were killed but not reaped within KILL_WAIT_SECONDS."""

    def __init__(self, limit=MAX_UNREAPED):
        self.limit = limit
        self._lock = threading.Lock()
        self._procs = []  # (key, proc)

    def _reap_locked(self):
        alive = []
        for key, proc in self._procs:
            try:
                done = proc.poll() is not None
            except Exception:  # be conservative: keep it counted
                done = False
            if not done:
                alive.append((key, proc))
        self._procs = alive

    def count(self):
        with self._lock:
            self._reap_locked()
            return len(self._procs)

    def blocked(self, key):
        with self._lock:
            self._reap_locked()
            return (len(self._procs) >= self.limit
                    or any(k == key for k, _ in self._procs))

    def add(self, key, proc):
        with self._lock:
            self._procs.append((key, proc))


DEFAULT_TRACKER = ChildTracker()


def run_command(argv, timeout, max_bytes, popen=subprocess.Popen,
                clock=time.monotonic, tracker=None):
    """Validate argv with _check() and run it bounded. Raises UnsafeCommand."""
    argv = _check(argv)
    tracker = DEFAULT_TRACKER if tracker is None else tracker
    key = argv[-1]
    if tracker.blocked(key):
        return RunResult("blocked_unreaped", None, "", 0.0, reaped=False)
    return _run_bounded(argv, timeout, max_bytes, popen, clock, tracker, key)


def _kill_and_wait(proc, wait_s):
    """SIGKILL then wait at most wait_s. True if the child was reaped."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=wait_s)
        return True
    except subprocess.TimeoutExpired:
        return False
    except OSError:
        return False


def _close(fh):
    try:
        fh.close()
    except OSError:
        pass


def _run_bounded(argv, timeout, max_bytes, popen, clock, tracker, key,
                 kill_wait=KILL_WAIT_SECONDS):
    """Generic bounded runner. Never raises for child misbehaviour.

    Private: production callers go through run_command(), which validates
    argv first. Tests use this directly with a harmless local child.
    """
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
    reaped = True
    deadline = start + timeout
    try:
        sel = selectors.DefaultSelector()
        try:
            sel.register(proc.stdout, selectors.EVENT_READ)
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
        except (OSError, ValueError):
            status = "read_error"
        finally:
            sel.close()

        if status == "ok":
            # stdout hit EOF; the child should exit promptly.
            try:
                proc.wait(timeout=max(0.1, deadline - clock()))
            except subprocess.TimeoutExpired:
                status = "timeout"
        if status != "ok":
            reaped = _kill_and_wait(proc, kill_wait)
            if not reaped:
                tracker.add(key, proc)
    except BaseException:
        # Unexpected failure: don't leave a running child behind.
        if proc.returncode is None and not _kill_and_wait(proc, kill_wait):
            tracker.add(key, proc)
        raise
    finally:
        _close(proc.stdout)

    text = bytes(buf[:max_bytes]).decode("utf-8", errors="replace")
    rc = proc.returncode if status == "ok" else None
    return RunResult(status, rc, text, clock() - start, reaped=reaped)
