#!/usr/bin/env bash
# lto-exporter <-> mhvtl integration test.
#
# Runs ONLY on a disposable Linux VM (the GitHub-hosted runner): it loads an
# out-of-tree kernel module (mhvtl), creates one emulated tape drive, runs
# the exporter against it as an unprivileged user under strace, and checks:
#   * exactly one emulated drive is identified, by synthetic serial AND by
#     its SCSI host being the mhvtl pseudo adapter;
#   * the exporter only exec'd `sg_logs --readonly --page=0x2|0x3 <that sg>`,
#     opened that node O_RDONLY, and only sent INQUIRY / LOG SENSE CDBs
#     (no LOG SELECT, no motion, no writes);
#   * metrics match what the emulator says it supports (unknown != zero).
# Do NOT run this on a host you care about.
#
# Required env: MHVTL_SRC (pinned, built checkout), LTO_VENV (venv with
# lto-exporter installed), EVIDENCE (output dir). Optional: PROMETHEUS_DIR.
set -euo pipefail

: "${MHVTL_SRC:?}" "${LTO_VENV:?}" "${EVIDENCE:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
SERIAL="GWCIXR0001"          # synthetic
QID=11
PORT=19842
WORK="$(mktemp -d /tmp/lto-mhvtl.XXXXXX)"
EXPORTER_USER=ltoexp
mkdir -p "$EVIDENCE"
chmod 755 "$WORK"

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*" | tee -a "$EVIDENCE/run.log"; }
fail() { log "FAIL: $*"; exit 1; }

VTLTAPE_PID=""; EXPORTER_PID=""; PROM_PID=""
cleanup() {
  set +e
  log "cleanup"
  [ -n "$PROM_PID" ] && kill "$PROM_PID" 2>/dev/null
  [ -n "$EXPORTER_PID" ] && kill "$EXPORTER_PID" 2>/dev/null
  [ -n "$VTLTAPE_PID" ] && kill "$VTLTAPE_PID" 2>/dev/null
  sleep 2
  [ -n "$VTLTAPE_PID" ] && kill -9 "$VTLTAPE_PID" 2>/dev/null
  if grep -q '^mhvtl ' /proc/modules; then
    rmmod mhvtl && log "mhvtl module removed" || log "rmmod mhvtl failed (runner is discarded anyway)"
  fi
  chmod -R a+rX "$EVIDENCE" 2>/dev/null
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || fail "must run as root (on a disposable VM)"
[ "${CI:-}" = "true" ] || [ "${LTO_MHVTL_I_UNDERSTAND:-}" = "yes" ] \
  || fail "refusing to run outside CI; set LTO_MHVTL_I_UNDERSTAND=yes on a throwaway VM"

# ---- 0. environment evidence -------------------------------------------
{
  echo "date: $(date -u +%FT%TZ)"
  echo "uname: $(uname -a)"
  echo "virt: $(systemd-detect-virt 2>/dev/null || echo unknown)"
  echo "dmi sys_vendor: $(cat /sys/class/dmi/id/sys_vendor 2>/dev/null || echo n/a)"
  echo "dmi product_name: $(cat /sys/class/dmi/id/product_name 2>/dev/null || echo n/a)"
  echo "mhvtl commit: $(git -C "$MHVTL_SRC" rev-parse HEAD)"
  echo "sg_logs: $(sg_logs --version 2>&1)"
  echo "strace: $(strace -V | head -1)"
  echo "python: $("$LTO_VENV/bin/python" --version)"
  echo "lto-exporter: $("$LTO_VENV/bin/lto-exporter" --version)"
} > "$EVIDENCE/environment.txt"
cat "$EVIDENCE/environment.txt"
lsscsi -g > "$EVIDENCE/lsscsi_before.txt" 2>&1 || true

if grep -qs '^mhvtl$' /sys/class/scsi_host/*/proc_name || grep -q '^mhvtl ' /proc/modules; then
  fail "an mhvtl adapter already exists; this harness only works on a clean VM"
fi

# ---- 1. load module, start ONE emulated drive ---------------------------
modprobe sg
insmod "$MHVTL_SRC/kernel/mhvtl.ko"
modinfo "$MHVTL_SRC/kernel/mhvtl.ko" > "$EVIDENCE/modinfo_mhvtl.txt"
grep '^mhvtl ' /proc/modules > "$EVIDENCE/proc_modules_mhvtl.txt"

mkdir -p "$WORK/conf" "$WORK/home" /var/lock/mhvtl
sed -e "s#@HOME@#$WORK/home#" -e "s#@SERIAL@#$SERIAL#" "$HERE/device.conf.in" > "$WORK/conf/device.conf"
printf 'MHVTL_HOME_PATH=%s\nCAPACITY=500\nVERBOSE=1\nVTL_DEBUG=0\n' "$WORK/home" > "$WORK/conf/mhvtl.conf"
cp "$WORK/conf/device.conf" "$EVIDENCE/device.conf"

log "starting vtltape -q $QID (no media, no library daemon)"
MHVTL_CONFIG_PATH="$WORK/conf" MHVTL_HOME_PATH="$WORK/home" \
  LD_LIBRARY_PATH="$MHVTL_SRC/usr" \
  "$MHVTL_SRC/usr/bin/vtltape" -F -q "$QID" -v1 > "$EVIDENCE/vtltape.log" 2>&1 &
VTLTAPE_PID=$!

# ---- 2. identify exactly our device -------------------------------------
SG=""
for _ in $(seq 1 60); do
  kill -0 "$VTLTAPE_PID" 2>/dev/null || fail "vtltape exited early (see vtltape.log)"
  if python3 "$HERE/identify.py" --serial "$SERIAL" --out "$EVIDENCE/identify.json" > "$WORK/sg" 2>>"$EVIDENCE/identify.log"; then
    SG="$(cat "$WORK/sg")"; break
  fi
  sleep 1
done
[ -n "$SG" ] || fail "emulated drive did not appear (see identify.log, vtltape.log)"
log "identified emulated drive: $SG"
lsscsi -g > "$EVIDENCE/lsscsi_after.txt" 2>&1 || true
lsscsi -H > "$EVIDENCE/lsscsi_hosts.txt" 2>&1 || true

# ---- 3. what does the emulator claim to support? (harness, not exporter)
# LOG SENSE of the Supported Log Pages page (0x00). Read-only, but it is
# the harness that runs it, outside the exporter's strace.
set +e
sg_logs --readonly --page=0x0 "$SG" > "$EVIDENCE/supported_pages.txt" 2>&1
echo "$?" > "$EVIDENCE/supported_pages.rc"
for pg in 0x2 0x3; do
  sg_logs --readonly --page=$pg "$SG" > "$EVIDENCE/harness_sg_logs_$pg.txt" 2>&1
  echo "$?" > "$EVIDENCE/harness_sg_logs_$pg.rc"
done
set -e
cat "$EVIDENCE/supported_pages.txt"

# ---- 4. least-privilege access: one node, one group ---------------------
id "$EXPORTER_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$EXPORTER_USER"
chgrp "$EXPORTER_USER" "$SG"
chmod 0640 "$SG"
{ ls -l "$SG"; id "$EXPORTER_USER"; } > "$EVIDENCE/permissions.txt"

cat > "$WORK/config.json" <<JSON
{
  "listen_address": "127.0.0.1",
  "port": $PORT,
  "command_timeout_seconds": 10,
  "cache_seconds": 0,
  "sg_logs_path": "$(command -v sg_logs)",
  "devices": [{"name": "mhvtl_drive0", "path": "$SG"}]
}
JSON
chmod 644 "$WORK/config.json"
cp "$WORK/config.json" "$EVIDENCE/exporter_config.json"

# ---- 5. exporter under strace, as the unprivileged user -----------------
log "starting exporter under strace as $EXPORTER_USER"
strace -f -y -o "$EVIDENCE/strace.txt" -xx -s 256 -qq \
  -e trace=execve,open,openat,openat2,ioctl,write,pwrite64,writev \
  -u "$EXPORTER_USER" \
  "$LTO_VENV/bin/lto-exporter" --config "$WORK/config.json" \
  > "$EVIDENCE/exporter.stdout" 2> "$EVIDENCE/exporter.stderr" &
EXPORTER_PID=$!
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/" >/dev/null 2>&1 && break
  sleep 1
done
for i in 1 2 3; do
  curl -fsS -o "$EVIDENCE/metrics_$i.txt" "http://127.0.0.1:$PORT/metrics" || fail "scrape $i failed"
  sleep 1
done
cat "$EVIDENCE/metrics_3.txt"

if [ -n "${PROMETHEUS_DIR:-}" ]; then
  "$PROMETHEUS_DIR/promtool" check metrics < "$EVIDENCE/metrics_3.txt" > "$EVIDENCE/promtool_check_metrics.txt" 2>&1 \
    || { cat "$EVIDENCE/promtool_check_metrics.txt"; fail "promtool check metrics"; }
  log "running Prometheus against the exporter to evaluate dashboard/alert queries"
  mkdir -p "$WORK/prom"
  cat > "$WORK/prom/prometheus.yml" <<YML
global: {scrape_interval: 2s, evaluation_interval: 2s}
rule_files: ["$ROOT/monitoring/prometheus/lto-exporter-alerts.yml"]
scrape_configs:
  - job_name: lto-exporter
    static_configs: [{targets: ["127.0.0.1:$PORT"]}]
YML
  "$PROMETHEUS_DIR/prometheus" --config.file="$WORK/prom/prometheus.yml" \
    --storage.tsdb.path="$WORK/prom/data" --web.listen-address=127.0.0.1:19090 \
    > "$EVIDENCE/prometheus.log" 2>&1 &
  PROM_PID=$!
  sleep 25
  python3 "$HERE/query_prometheus.py" --url http://127.0.0.1:19090 \
    --supported "$EVIDENCE/supported_pages.txt" --out "$EVIDENCE/prometheus_queries.json" \
    || fail "dashboard/alert queries against live Prometheus"
  kill "$PROM_PID"; PROM_PID=""
fi

kill "$EXPORTER_PID"; wait "$EXPORTER_PID" 2>/dev/null || true; EXPORTER_PID=""

# ---- 6. verdict ----------------------------------------------------------
python3 "$HERE/check_evidence.py" --evidence "$EVIDENCE" --sg "$SG" \
  --sg-logs "$(command -v sg_logs)"
