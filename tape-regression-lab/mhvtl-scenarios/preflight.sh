#!/usr/bin/env bash
# Can this host run the mhvtl scenarios? Exit 0 = yes, 77 = skip (reason on
# stdout as "skip: ..."), anything else = the check itself broke.
#
#   preflight.sh --probe        before building: can we build and insmod an
#                               out-of-tree module here at all?
#   preflight.sh MHVTL_SRC      before running: the built module is there too
#
# Only *static* host properties lead to a skip. Once preflight passes, every
# later failure (insmod refused, drive never appears, an assertion) is a FAIL:
# a runner that should be able to do this and suddenly can't is a regression,
# not something to hide.
set -u
skip() { echo "skip: $*"; exit 77; }

[ "$(uname -s)" = Linux ] || skip "not Linux ($(uname -s))"
if command -v systemd-detect-virt >/dev/null 2>&1; then
  c="$(systemd-detect-virt --container 2>/dev/null)"
  [ -z "$c" ] || [ "$c" = none ] || skip "running in a container ($c): cannot load kernel modules"
fi
[ -e /.dockerenv ] && skip "running in a container (/.dockerenv): cannot load kernel modules"
if [ "$(cat /proc/sys/kernel/modules_disabled 2>/dev/null || echo 0)" = 1 ]; then
  skip "kernel.modules_disabled=1"
fi
if grep -qE '\[(integrity|confidentiality)\]' /sys/kernel/security/lockdown 2>/dev/null; then
  skip "kernel lockdown is on ($(cat /sys/kernel/security/lockdown)): unsigned modules are refused"
fi
if [ "$(id -u)" != 0 ] && ! sudo -n true 2>/dev/null; then
  skip "neither root nor passwordless sudo"
fi

if [ "${1:-}" = --probe ]; then
  kver="$(uname -r)"
  if [ ! -d "/lib/modules/$kver/build" ]; then
    command -v apt-cache >/dev/null 2>&1 || skip "no headers for $kver and no apt to fetch them"
    cand="$(apt-cache policy "linux-headers-$kver" 2>/dev/null | awk '/Candidate:/ {print $2}')"
    [ -n "$cand" ] && [ "$cand" != "(none)" ] || skip "no kernel headers package for $kver"
  fi
  echo "available: $(uname -r)"
  exit 0
fi

src="${1:?usage: preflight.sh --probe | MHVTL_SRC}"
[ -f "$src/kernel/mhvtl.ko" ] || skip "mhvtl.ko not built at $src/kernel"
for t in sg_logs sg_raw sg_turs strace curl python3; do
  command -v "$t" >/dev/null 2>&1 || skip "missing tool: $t"
done
echo "available: $(uname -r)"
