#!/bin/sh
# Thin wrapper: resolve this kit's directory and run the stdlib-only runner.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 2
command -v python3 >/dev/null 2>&1 || { echo "run.sh: python3 not found (need Python 3.8+)" >&2; exit 2; }
exec python3 -I "$here/run.py" "$@"
