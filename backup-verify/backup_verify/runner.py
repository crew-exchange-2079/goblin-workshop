"""Bounded subprocess runner.

No shell is ever used. Each child gets a minimal environment (PATH, LC_ALL
and only the variables a target explicitly passes through), its own process
group so a timeout kills grandchildren too, and stdout/stderr spooled to
unlinked temporary files so a chatty child can neither deadlock a pipe nor
grow this process's memory. Callers read stdout back up to a cap, or stream
it line by line (restic ls on a large snapshot).
"""

import json
import os
import re
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass

BASE_ENV = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "LC_ALL": "C"}
# What the docker/podman CLI needs to find its daemon and credentials.
CONTAINER_ENV = ("HOME", "DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT",
                 "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR",
                 "CONTAINER_HOST")
STDERR_TAIL_BYTES = 4096
_ERROR_RE = re.compile(r"\b(error|fatal|panic)\b", re.IGNORECASE)
KILL_WAIT_SECONDS = 5.0


def build_env(pass_env=(), extra=None, environ=None):
    environ = os.environ if environ is None else environ
    env = dict(BASE_ENV)
    for name in pass_env:
        if name in environ:
            env[name] = environ[name]
    env.update(extra or {})
    return env


@dataclass
class Result:
    argv: list
    returncode: int | None   # None: timed out or could not start
    stdout: bytes            # at most max_stdout bytes
    stdout_truncated: bool
    stderr_tail: str
    duration: float
    timed_out: bool = False
    start_error: str = ""

    @property
    def ok(self):
        return self.returncode == 0

    def describe(self):
        """One-line reason for a failure, for reports."""
        name = os.path.basename(self.argv[0]) if self.argv else "?"
        if self.start_error:
            return f"{name}: {self.start_error}"
        if self.timed_out:
            return f"{name}: timed out after {self.duration:.0f}s"
        # The most telling line: the last one that says error/fatal (psql
        # follows "ERROR: ..." with the query and a caret), else the last.
        tail = [ln.strip() for ln in self.stderr_tail.splitlines() if ln.strip()]
        errors = [ln for ln in tail if _ERROR_RE.search(ln)]
        last = _unjson((errors or tail or [""])[-1])[:500]
        return f"{name}: exit {self.returncode}" + (f": {last}" if last else "")


def run(argv, timeout, env=None, max_stdout=16 << 20, stdin=None, stdout_sink=None,
        work_dir=None, popen=subprocess.Popen):
    """Run argv bounded by timeout. Never raises for child misbehaviour.

    stdin: None (devnull), bytes, or a binary file object to feed the child.
    stdout_sink: optional callable taking a binary file positioned at the
    start of the child's full stdout, called after the child exits (only if
    it exited 0); its return value is ignored and Result.stdout stays empty.
    """
    start = time.monotonic()
    env = BASE_ENV if env is None else env
    with tempfile.TemporaryFile(dir=work_dir) as out, \
            tempfile.TemporaryFile(dir=work_dir) as err:
        stdin_arg = subprocess.DEVNULL
        if isinstance(stdin, (bytes, bytearray)):
            stdin_file = tempfile.TemporaryFile(dir=work_dir)
            stdin_file.write(stdin)
            stdin_file.seek(0)
            stdin_arg = stdin_file
        elif stdin is not None:
            stdin_file = None
            stdin_arg = stdin
        else:
            stdin_file = None
        try:
            try:
                proc = popen(list(argv), stdin=stdin_arg, stdout=out, stderr=err,
                             env=env, shell=False, close_fds=True,
                             start_new_session=True)
            except OSError as e:
                return Result(list(argv), None, b"", False, "",
                              time.monotonic() - start, start_error=e.strerror or str(e))
            timed_out = False
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
        finally:
            if stdin_file is not None:
                stdin_file.close()
        duration = time.monotonic() - start
        err.seek(0, os.SEEK_END)
        size = err.tell()
        err.seek(max(0, size - STDERR_TAIL_BYTES))
        stderr_tail = err.read().decode("utf-8", "replace")
        rc = None if timed_out else proc.returncode
        out.seek(0)
        if stdout_sink is not None:
            if rc == 0:
                stdout_sink(out)
            return Result(list(argv), rc, b"", False, stderr_tail, duration, timed_out)
        data = out.read(max_stdout + 1)
        truncated = len(data) > max_stdout
        return Result(list(argv), rc, data[:max_stdout], truncated, stderr_tail,
                      duration, timed_out)


def _unjson(line):
    """restic --json reports fatal errors as {"message_type":"exit_error",...}."""
    if line.startswith("{"):
        try:
            msg = json.loads(line).get("message")
        except (ValueError, AttributeError):
            return line
        if isinstance(msg, str):
            return msg
    return line


def _kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass
