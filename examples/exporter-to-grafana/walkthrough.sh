#!/usr/bin/env bash
# exporter -> Prometheus -> Grafana, synthetic data. Needs Docker with the
# compose plugin, and python3. Leaves the stack running so you can open
# Grafana; stop it with `docker compose down` (or pass --down).
set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\n\033[1;36m$ %s\033[0m\n' "$*"; "$@"; }

step docker compose --progress quiet up -d --quiet-pull
step python3 peek.py wait
step python3 peek.py metrics
step python3 peek.py targets
step python3 peek.py query 'rate(tape_data_bytes_total{device="drive0"}[30s])'
step python3 peek.py alerts
step python3 peek.py dashboard

if [ "${1:-}" = "--down" ]; then
  step docker compose --progress quiet down --volumes
else
  printf '\nGrafana: http://127.0.0.1:%s/d/lto-exporter-v1  (stop: docker compose down)\n' "${GRAFANA_PORT:-3000}"
fi
