#!/usr/bin/env bash
# tape-regression-lab: scripted mhvtl scenarios against lto-exporter.
#
# Runs ONLY on a disposable Linux VM (the GitHub-hosted runner): it loads the
# mhvtl kernel module, creates one emulated drive and media, MOVES EMULATED
# TAPE (write/read/rewind/space), and flips the emulated device offline. The
# exporter itself runs as an unprivileged user under strace and is only
# scraped; everything that changes drive state is the harness, as root.
# Do NOT run this on a host you care about.
#
# Required env: MHVTL_SRC (pinned, built checkout), LTO_VENV (venv with
# lto-exporter installed), EVIDENCE (new output dir).
# Optional: MHVTL_SCENARIOS_REQUIRE=1 turns a preflight skip into a failure
# (CI sets it once its own probe said the runner can do this).
# Optional: SCENARIOS="load_unload worm" runs only those (baseline always runs).
#
# Exit: 0 all scenarios PASS, 1 FAIL, 77 skipped (host can't load modules).
set -euo pipefail

: "${MHVTL_SRC:?}" "${LTO_VENV:?}" "${EVIDENCE:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IDENTIFY="$REPO/tape-exporter/ci/mhvtl/identify.py"
SERIAL="GWCISC0001"          # synthetic
LIB_ID=10
QID=11
PORT=19843
EXPORTER_USER=ltoexp
mkdir -p "$EVIDENCE"

set +e
reason="$("$HERE/preflight.sh" "$MHVTL_SRC")"
rc=$?
set -e
if [ "$rc" != 0 ]; then
  echo "$reason"
  printf '# mhvtl scenarios: SKIPPED\n\n%s\n' "$reason" > "$EVIDENCE/summary.md"
  if [ "$rc" = 77 ] && [ "${MHVTL_SCENARIOS_REQUIRE:-0}" != 1 ]; then exit 77; fi
  exit 1
fi
[ "$(id -u)" = 0 ] || { echo "must run as root (on a disposable VM)"; exit 1; }
[ "${CI:-}" = "true" ] || [ "${LTO_MHVTL_I_UNDERSTAND:-}" = "yes" ] \
  || { echo "refusing to run outside CI; set LTO_MHVTL_I_UNDERSTAND=yes on a throwaway VM"; exit 1; }

# Refuse a host that already has mhvtl BEFORE creating anything or installing
# the EXIT trap: cleanup must never touch a module or daemon this run didn't
# start.
if grep -qs '^mhvtl$' /sys/class/scsi_host/*/proc_name || grep -q '^mhvtl ' /proc/modules; then
  echo "FAIL: an mhvtl adapter/module already exists; this harness only works on a clean VM (nothing was changed)"
  exit 1
fi

WORK="$(mktemp -d /tmp/lto-scen.XXXXXX)"
chmod 755 "$WORK"
log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$EVIDENCE/run.log"; }
fail() { log "FAIL: $*"; exit 1; }

# Everything cleanup may touch is recorded here when THIS run creates it.
VTLTAPE_PID=""; EXPORTER_PID=""; SG=""; INSERTED_MODULE=0
wait_gone() { local pid=$1 n=${2:-20}; while [ "$n" -gt 0 ] && kill -0 "$pid" 2>/dev/null; do sleep 1; n=$((n-1)); done; ! kill -0 "$pid" 2>/dev/null; }

# Descendants of a pid (children first-level and below), never by user name:
# an ltoexp account may pre-exist and run things that aren't ours.
descendants() { local c; for c in $(pgrep -P "$1"); do echo "$c"; descendants "$c"; done; }

stop_exporter() {
  [ -n "$EXPORTER_PID" ] || return 0
  # SIGTERM the exporter (strace's child), let strace flush and exit.
  local kids; kids="$(pgrep -P "$EXPORTER_PID" || true)"
  [ -z "$kids" ] || kill -TERM $kids 2>/dev/null
  if ! wait_gone "$EXPORTER_PID" 15; then
    kids="$(descendants "$EXPORTER_PID")"
    [ -z "$kids" ] || kill -KILL $kids 2>/dev/null
    kill -KILL "$EXPORTER_PID" 2>/dev/null
    wait_gone "$EXPORTER_PID" 5
  fi
  EXPORTER_PID=""
}

cleanup() {
  set +e
  log "cleanup"
  stop_exporter
  if [ -n "$SG" ] && [ -w "/sys/class/scsi_generic/${SG#/dev/}/device/state" ]; then
    echo running > "/sys/class/scsi_generic/${SG#/dev/}/device/state"
  fi
  if [ -n "$VTLTAPE_PID" ]; then
    kill "$VTLTAPE_PID" 2>/dev/null
    wait_gone "$VTLTAPE_PID" 10 || kill -9 "$VTLTAPE_PID" 2>/dev/null
    wait_gone "$VTLTAPE_PID" 5 || log "vtltape did not exit"
  fi
  ps -eo pid,user,stat,wchan:32,args | grep -E "vtltape|lto-exporter|sg_logs|sg_raw|strace" | grep -v grep > "$EVIDENCE/ps_at_cleanup.txt"
  if [ "$INSERTED_MODULE" = 1 ] && grep -q '^mhvtl ' /proc/modules; then
    timeout -k 5 20 rmmod mhvtl && log "mhvtl module removed" || log "rmmod mhvtl failed/timed out (VM is discarded)"
  fi
  chmod -R a+rX "$EVIDENCE" 2>/dev/null
}
trap cleanup EXIT

{
  echo "date: $(date -u +%FT%TZ)"
  echo "uname: $(uname -a)"
  echo "virt: $(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "mhvtl commit: $(git -C "$MHVTL_SRC" rev-parse HEAD)"
  echo "sg_logs: $(sg_logs --version 2>&1)"
  echo "sg_raw: $(sg_raw --version 2>&1)"
  echo "strace: $(strace -V | head -1)"
  echo "lto-exporter: $("$LTO_VENV/bin/lto-exporter" --version)"
} > "$EVIDENCE/environment.txt"
cat "$EVIDENCE/environment.txt"

# ---- 1. module, ONE drive, no media yet ----------------------------------
modprobe sg
insmod "$MHVTL_SRC/kernel/mhvtl.ko" || fail "insmod mhvtl.ko refused (preflight said it should work)"
INSERTED_MODULE=1
mkdir -p "$WORK/conf" "$WORK/home" /var/lock/mhvtl
sed -e "s#@HOME@#$WORK/home#" -e "s#@SERIAL@#$SERIAL#" "$HERE/device.conf.in" > "$WORK/conf/device.conf"
printf 'MHVTL_HOME_PATH=%s\nCAPACITY=500\nVERBOSE=1\nVTL_DEBUG=0\n' "$WORK/home" > "$WORK/conf/mhvtl.conf"
cp "$WORK/conf/device.conf" "$EVIDENCE/device.conf"

log "starting vtltape -q $QID"
MHVTL_CONFIG_PATH="$WORK/conf" MHVTL_HOME_PATH="$WORK/home" LD_LIBRARY_PATH="$MHVTL_SRC/usr" \
  "$MHVTL_SRC/usr/bin/vtltape" -F -q "$QID" -v1 > "$EVIDENCE/vtltape.log" 2>&1 &
VTLTAPE_PID=$!

for _ in $(seq 1 60); do
  kill -0 "$VTLTAPE_PID" 2>/dev/null || fail "vtltape exited early (see vtltape.log)"
  if python3 "$IDENTIFY" --serial "$SERIAL" --out "$EVIDENCE/identify.json" > "$WORK/sg" 2>>"$EVIDENCE/identify.log"; then
    SG="$(cat "$WORK/sg")"; break
  fi
  sleep 1
done
[ -n "$SG" ] || fail "emulated drive did not appear (see identify.log, vtltape.log)"
log "identified emulated drive: $SG"
lsscsi -g > "$EVIDENCE/lsscsi.txt" 2>&1 || true

# A fresh drive answers its first command with UNIT ATTENTION (power on).
# Consume it here, as the harness, so the exporter's first read is clean.
for _ in 1 2 3 4 5; do
  set +e; sg_turs "$SG" >> "$EVIDENCE/initial_tur.txt" 2>&1; trc=$?; set -e
  echo "rc=$trc" >> "$EVIDENCE/initial_tur.txt"
  [ "$trc" = 6 ] || break
done

# ---- 2. exporter under strace, unprivileged, read access to one node -----
id "$EXPORTER_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$EXPORTER_USER"
chgrp "$EXPORTER_USER" "$SG"
chmod 0640 "$SG"
cat > "$WORK/config.json" <<JSON
{
  "listen_address": "127.0.0.1",
  "port": $PORT,
  "command_timeout_seconds": 20,
  "cache_seconds": 0,
  "sg_logs_path": "$(command -v sg_logs)",
  "devices": [{"name": "mhvtl_drive0", "path": "$SG"}]
}
JSON
chmod 644 "$WORK/config.json"
cp "$WORK/config.json" "$EVIDENCE/exporter_config.json"

log "starting exporter under strace as $EXPORTER_USER"
strace -f -y -o "$EVIDENCE/strace.txt" -xx -s 256 -qq \
  -e trace=execve,open,openat,openat2,ioctl,write,pwrite64,writev \
  -u "$EXPORTER_USER" \
  "$LTO_VENV/bin/lto-exporter" --config "$WORK/config.json" \
  > "$EVIDENCE/exporter.stdout" 2> "$EVIDENCE/exporter.stderr" &
EXPORTER_PID=$!
for _ in $(seq 1 30); do
  curl -fsS --max-time 60 "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 1
done
curl -fsS --max-time 60 "http://127.0.0.1:$PORT/" >/dev/null || fail "exporter did not come up"

# ---- 3. scenarios (harness as root; exporter only scraped) --------------
only=()
for s in ${SCENARIOS:-}; do only+=(--only "$s"); done
[ "${#only[@]}" = 0 ] || only=(--only baseline "${only[@]}")
log "running scenarios ${SCENARIOS:-(all)}"
timeout -k 10 900 python3 "$HERE/scenarios.py" \
  --sg "$SG" --sg-logs "$(command -v sg_logs)" --metrics-url "http://127.0.0.1:$PORT/metrics" \
  --mhvtl-src "$MHVTL_SRC" --conf "$WORK/conf" --home "$WORK/home" --work "$WORK" \
  --evidence "$EVIDENCE" --drive-id "$QID" --library-id "$LIB_ID" "${only[@]}" \
  || log "scenario driver exited non-zero (verdict will say why)"

log "stopping exporter"
stop_exporter

# ---- 4. verdict: scenario checks + whole-run exporter strace -------------
timeout -k 5 60 python3 "$HERE/scenarios.py" verdict --evidence "$EVIDENCE" --sg "$SG" \
  --sg-logs "$(command -v sg_logs)"
