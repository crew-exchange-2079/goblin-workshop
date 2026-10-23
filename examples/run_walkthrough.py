"""Run one examples/ walkthrough end to end, check its output, and tear it down.

  python3 examples/run_walkthrough.py exporter-to-grafana [--cast out.cast]

Runs <walkthrough>/walkthrough.sh --down inside a pseudo-terminal (so the
output is what a person sees), under a unique compose project name and free
127.0.0.1 ports, so it never touches a stack you already have running. Then:

  * the script must exit 0 and its output must contain every line listed in
    EXPECT for that walkthrough (and none of the containers may be left);
  * `docker compose down --volumes` runs in `finally`, also on failure;
  * with --cast, the session is written as an asciicast v2 recording with its
    real timings. The committed <walkthrough>/walkthrough.cast is made this way.

Exit: 0 pass, 1 check failed, 2 could not run. Standard library only, Linux/macOS.
"""

import argparse
import json
import os
import pty
import re
import select
import socket
import subprocess
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
WIDTH, HEIGHT = 132, 36
TIMEOUT = 600

# Lines (regular expressions) each walkthrough's output must contain. These are
# the claims its README makes; the synthetic targets are deterministic in shape.
# Alert state is matched as pending OR firing: which one depends on how long the
# run has been up relative to each rule's `for:`, not on whether it is correct.
EXPECT = {
    "exporter-to-grafana": [
        r"stack ready: Prometheus scrapes 2 exporters, Grafana is up",
        r'tape_cleaning_required\{device="drive1"\} 1',
        r"lto-exporter\s+exporter:9101\s+up",
        r"lto-exporter\s+exporter:9102\s+up",
        r"device=drive0,direction=write,instance=exporter:9101,side=host\s+[1-9]",
        r"LTOCleaningRequired\s+(pending|firing)\s+device=drive1",
        r"LTODeviceChanged\s+(pending|firing)\s+device=drive3.*status=device_missing",
        r"dashboard: LTO tape drives \(lto-exporter\) \(\d+ panels\)",
        r"device=drive1\s+cleaning_required=1",
    ],
}
PORT_VARS = ("EXPORTER_PORT", "PROMETHEUS_PORT", "GRAFANA_PORT")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_in_pty(argv, cwd, env, cast):
    """Run argv on a pty; return (exit code, output). Optionally record events."""
    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(cwd)
        os.execvpe(argv[0], argv, env)
    start = time.monotonic()
    chunks = []
    try:
        while True:
            if time.monotonic() - start > TIMEOUT:
                os.kill(pid, 9)
                raise TimeoutError(f"walkthrough did not finish in {TIMEOUT}s")
            ready, _, _ = select.select([fd], [], [], 1)
            if not ready:
                continue
            try:
                data = os.read(fd, 4096)
            except OSError:      # EIO: the child closed the pty
                break
            if not data:
                break
            text = data.decode("utf-8", "replace")
            chunks.append(text)
            if cast is not None:
                cast.append([round(time.monotonic() - start, 3), "o", text])
    finally:
        os.close(fd)
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), "".join(chunks)


def compose(project, cwd, env, *args):
    return subprocess.run(["docker", "compose", "-p", project, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=180)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("walkthrough", choices=sorted(EXPECT))
    ap.add_argument("--cast", help="write an asciicast v2 recording here")
    args = ap.parse_args(argv)

    cwd = os.path.join(HERE, args.walkthrough)
    project = f"walkthrough-{uuid.uuid4().hex[:8]}"
    env = dict(os.environ, COMPOSE_PROJECT_NAME=project, TERM="xterm-256color",
               COLUMNS=str(WIDTH), LINES=str(HEIGHT))
    if not args.cast:   # a recording keeps the documented default ports
        env.update({k: str(free_port()) for k in PORT_VARS})
    events = [] if args.cast else None
    try:
        code, out = run_in_pty(["bash", "walkthrough.sh", "--down"], cwd, env, events)
    except (OSError, TimeoutError) as e:
        print(f"could not run {args.walkthrough}: {e}", file=sys.stderr)
        compose(project, cwd, env, "down", "--volumes")
        return 2
    finally:
        left = compose(project, cwd, env, "ps", "-aq").stdout.strip()
        compose(project, cwd, env, "down", "--volumes")

    sys.stdout.write(out)
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out).replace("\r", "")
    failures = [f"exit code {code}"] if code != 0 else []
    failures += [f"missing: {pat}" for pat in EXPECT[args.walkthrough]
                 if not re.search(pat, plain)]
    if left:
        failures.append(f"walkthrough.sh --down left containers: {left.split()}")
    if args.cast and not failures:
        with open(args.cast, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"version": 2, "width": WIDTH, "height": HEIGHT,
                                 "title": f"examples/{args.walkthrough}",
                                 "env": {"TERM": "xterm-256color", "SHELL": "/bin/bash"}}) + "\n")
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        print(f"\nrecorded {args.cast}")
    for f in failures:
        print(f"FAIL {args.walkthrough}: {f}", file=sys.stderr)
    if not failures:
        print(f"\nPASS {args.walkthrough}: {len(EXPECT[args.walkthrough])} output checks")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
