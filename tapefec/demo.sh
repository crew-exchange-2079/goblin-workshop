#!/bin/sh
# SPDX-License-Identifier: MIT
# Streaming recovery demo: every stage is a separate stdin->stdout process.
#
#   sh demo.sh [MB]          # default 20 MB of random input
#
#   encode | damage --per-group 4 | decode    -> exactly m losses: exit 0
#   encode | damage --per-group 5 | decode    -> m+1 losses:       exit 1
#
# `tapefec damage` drops or corrupts frames inside every group of 24 on the
# way through, the way a tape with bad blocks would.  Nothing is written to
# disk between the stages; only the input and the restored output are files.
# Set PYTHON to pick an interpreter, TAPEFEC_ENGINE=pure|numpy to pick the
# Reed-Solomon engine.
set -eu

mb=${1:-20}
py=${PYTHON:-python3}
here=$(cd "$(dirname "$0")" && pwd)
fec="$py $here/tapefec.py"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT INT TERM

"$py" -c "import random, sys
sys.stdout.buffer.write(random.Random(20261023).randbytes($mb * 1000000))" \
    > "$tmp/original"

echo "== $mb MB in; 4 of every 24 frames destroyed in transit (k=20 m=4)"
$fec encode < "$tmp/original" \
    | $fec damage --per-group 4 --seed 1 \
    | $fec decode --text > "$tmp/restored" 2> "$tmp/report"
grep -v '^{' "$tmp/report" | head -n 5
if cmp -s "$tmp/original" "$tmp/restored"; then
    echo "-> exit status 0, output is byte-identical to the input"
else
    echo "-> MISMATCH: restored output differs from the input" >&2
    exit 1
fi

echo
echo "== same again with 5 of every 24 destroyed: one more than m"
rc=0
$fec encode < "$tmp/original" \
    | $fec damage --per-group 5 --seed 1 \
    | $fec decode --text > "$tmp/restored" 2> "$tmp/report" || rc=$?
grep -v '^{' "$tmp/report" | head -n 3
"$py" -c "import json, sys
doc = json.loads(open(sys.argv[1]).read().strip().splitlines()[-1])
r = doc['unrecoverable_ranges']
print('-> %d unrecoverable range(s), first %s; output still %d bytes long'
      % (len(r), r[0], doc['bytes_written']))" "$tmp/report"
echo "-> exit status $rc"
[ "$rc" -eq 1 ]
