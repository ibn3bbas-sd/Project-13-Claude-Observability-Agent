#!/bin/bash
# Deploy Prometheus + Loki observability stack to monitoring namespace
# Run this on the OKE jumpbox (has direct kubectl access)
set -e

NAMESPACE="monitoring"

echo "=== Checking / installing Helm ==="
if ! command -v helm &>/dev/null; then
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi
helm version

echo "=== Adding Helm repositories ==="
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add grafana https://grafana.github.io/helm-charts
helm repo update

# ---------------------------------------------------------------------------
# Prometheus + Grafana + kube-state-metrics + node-exporter
# After install: http://prom-stack-kube-prometheus-prometheus.monitoring:9090
# ---------------------------------------------------------------------------
echo "=== Deploying Prometheus stack (prom-stack) ==="
helm upgrade --install prom-stack prometheus-community/kube-prometheus-stack \
  -n "$NAMESPACE" \
  --set alertmanager.enabled=false \
  --set grafana.enabled=true \
  --set grafana.adminPassword="${GRAFANA_ADMIN_PASSWORD:?set GRAFANA_ADMIN_PASSWORD}" \
  --set grafana.service.type=ClusterIP \
  --set grafana.persistence.enabled=false \
  --set prometheus.prometheusSpec.retention=7d \
  --set prometheus.prometheusSpec.scrapeInterval=30s \
  --set 'prometheus.prometheusSpec.resources.requests.cpu=100m' \
  --set 'prometheus.prometheusSpec.resources.requests.memory=256Mi' \
  --set 'prometheus.prometheusSpec.resources.limits.cpu=500m' \
  --set 'prometheus.prometheusSpec.resources.limits.memory=1Gi' \
  --set 'prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce' \
  --set 'prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=10Gi' \
  --set prometheusOperator.admissionWebhooks.enabled=false \
  --set nodeExporter.enabled=true \
  --set kubeStateMetrics.enabled=true \
  --timeout 10m \
  --wait

echo "✓ Prometheus stack deployed"

# ---------------------------------------------------------------------------
# Loki (log store) + Promtail DaemonSet (ships every pod's logs automatically)
# After install: http://loki-stack.monitoring:3100
# ---------------------------------------------------------------------------
echo "=== Deploying Loki + Promtail (loki-stack) ==="
helm upgrade --install loki-stack grafana/loki-stack \
  -n "$NAMESPACE" \
  --set loki.enabled=true \
  --set loki.persistence.enabled=true \
  --set loki.persistence.size=10Gi \
  --set 'loki.resources.requests.cpu=50m' \
  --set 'loki.resources.requests.memory=128Mi' \
  --set 'loki.resources.limits.cpu=200m' \
  --set 'loki.resources.limits.memory=512Mi' \
  --set promtail.enabled=true \
  --set 'promtail.config.clients[0].url=http://loki-stack.monitoring:3100/loki/api/v1/push' \
  --set 'promtail.resources.requests.cpu=30m' \
  --set 'promtail.resources.requests.memory=64Mi' \
  --set grafana.enabled=false \
  --set fluent-bit.enabled=false \
  --timeout 5m \
  --wait

echo "✓ Loki + Promtail deployed"

echo ""
echo "=== Verifying pods ==="
kubectl -n "$NAMESPACE" get pods -o wide

echo ""
echo "=== Services ==="
kubectl -n "$NAMESPACE" get svc

echo ""
echo "Prometheus: http://prom-stack-kube-prometheus-prometheus.monitoring:9090"
echo "Loki:       http://loki-stack.monitoring:3100"
echo "Grafana:    http://prom-stack-grafana.monitoring:80  (admin / password in the grafana-admin Secret)"
