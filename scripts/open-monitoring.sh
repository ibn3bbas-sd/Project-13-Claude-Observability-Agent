#!/usr/bin/env bash
# Copyright 2026 Ibn Abbas
# SPDX-License-Identifier: Apache-2.0
# open-monitoring.sh
#
# Opens browser access to Grafana, Prometheus, and Loki via kubectl port-forward.
# Requires an active bastion tunnel on localhost:6443.
#
# Usage (from WSL):
#   bash scripts/open-monitoring.sh [--start-tunnel]
#
#   --start-tunnel   Also start the 6443 bastion tunnel (blocks; run in its own terminal).
#                    Without this flag, assumes the tunnel is already running.
#
# Access after running:
#   Grafana    → http://localhost:3000   (admin / password in the grafana-admin Secret)
#   Prometheus → http://localhost:9090
#   Loki       → http://localhost:3100

set -euo pipefail

NAMESPACE="monitoring"
GRAFANA_SVC="monitoring-grafana"
PROMETHEUS_SVC="prom-stack-kube-prometheus-prometheus"
LOKI_SVC="loki-stack"

TUNNEL_SCRIPT="${TUNNEL_SCRIPT:-$HOME/bastion/oke-jumpbox.sh}"

# ── helpers ──────────────────────────────────────────────────────────────────

log() { echo "[$(date +%H:%M:%S)] $*"; }

wait_for_port() {
  local port=$1 label=$2 retries=30
  log "Waiting for $label on localhost:$port ..."
  until nc -z localhost "$port" 2>/dev/null; do
    retries=$((retries - 1))
    [ "$retries" -eq 0 ] && { log "ERROR: $label never became available on :$port"; return 1; }
    sleep 2
  done
  log "$label is ready → http://localhost:$port"
}

cleanup() {
  log "Shutting down port-forwards ..."
  kill "${PF_PIDS[@]}" 2>/dev/null || true
  [ -n "${TUNNEL_PID:-}" ] && kill "$TUNNEL_PID" 2>/dev/null || true
  log "Done."
}

# ── optional: start bastion tunnel ───────────────────────────────────────────

TUNNEL_PID=""
if [[ "${1:-}" == "--start-tunnel" ]]; then
  log "Starting bastion tunnel localhost:6443 → OKE API ..."
  bash "$TUNNEL_SCRIPT" --port-forward 6443:6443 &
  TUNNEL_PID=$!
  log "Tunnel PID: $TUNNEL_PID — waiting up to 60 s for API server ..."
  wait_for_port 6443 "K8s API"
fi

# ── verify cluster connectivity ───────────────────────────────────────────────

if ! kubectl cluster-info --request-timeout=5s &>/dev/null; then
  log "ERROR: kubectl cannot reach the cluster."
  log "Start the tunnel first:"
  log "  wsl bash $TUNNEL_SCRIPT --port-forward 6443:6443"
  exit 1
fi
log "Cluster reachable."

# ── start port-forwards ───────────────────────────────────────────────────────

trap cleanup EXIT INT TERM

PF_PIDS=()

log "Port-forwarding Grafana    → localhost:3000"
kubectl port-forward -n "$NAMESPACE" "svc/$GRAFANA_SVC" 3000:80 &>/tmp/pf-grafana.log &
PF_PIDS+=($!)

log "Port-forwarding Prometheus → localhost:9090"
kubectl port-forward -n "$NAMESPACE" "svc/$PROMETHEUS_SVC" 9090:9090 &>/tmp/pf-prometheus.log &
PF_PIDS+=($!)

log "Port-forwarding Loki       → localhost:3100"
kubectl port-forward -n "$NAMESPACE" "svc/$LOKI_SVC" 3100:3100 &>/tmp/pf-loki.log &
PF_PIDS+=($!)

# wait a moment then verify
sleep 3
wait_for_port 3000 "Grafana"
wait_for_port 9090 "Prometheus"
wait_for_port 3100 "Loki"

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║           Monitoring Stack — Browser Access          ║"
echo "╠══════════════════════════════════════════════════════╣"
echo "║  Grafana    → http://localhost:3000                  ║"
echo "║              Login: admin / <grafana-admin-password> ║"
echo "║  Prometheus → http://localhost:9090                  ║"
echo "║  Loki       → http://localhost:3100                  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "Press Ctrl+C to stop all port-forwards."

# keep script alive
wait "${PF_PIDS[@]}"
