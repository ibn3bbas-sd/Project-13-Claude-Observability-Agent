# Claude Observability Agent (OCI OKE)

A Claude-powered Kubernetes observability agent for Oracle Kubernetes Engine (OKE), plus the
Prometheus / Grafana / Loki / Tempo / OpenTelemetry stack it reads from. This file collects the
architecture, operational runbooks, and lessons learned for working on the repo.

> All hostnames, IPs, and OCIDs below are **examples** (`example.com`, `10.0.x.x`,
> `203.0.113.x`, `ocid1.<type>.oc1.us-ashburn-1.aaaaaaaaexample...`). Substitute your own.

---

## Cluster Access via OCI Bastion

The OKE API endpoint is private. `kubectl` reaches it through an OCI Bastion port-forward session
to a jumpbox inside the VCN. A small wrapper script (example: `~/bastion/oke-jumpbox.sh`) creates
or reuses a Bastion session and opens the tunnel; a second one (`~/bastion/infra-jumpbox.sh`) is
used for an OCI-CLI jumpbox when managing infrastructure (load balancers, etc.).

| Jumpbox | Example script | Example IP | Purpose |
| ------- | -------------- | ---------- | ------- |
| OCI infra | `~/bastion/infra-jumpbox.sh` | `10.0.1.10` | OCI CLI, LB scripts |
| OKE | `~/bastion/oke-jumpbox.sh` | `10.0.1.11` | `kubectl` tunnel to the cluster API |

SSH key used for Bastion sessions: `~/.ssh/bastion_key` (+ `.pub`).

**Workflow**

```bash
# 1. Start the tunnel (background); first session can take ~30 s to become ACTIVE
bash ~/bastion/oke-jumpbox.sh --port-forward 6443:6443 &
TUNNEL_PID=$!

# 2. Verify
kubectl cluster-info

# 3. Apply / deploy
kubectl apply -f <manifest>
kubectl rollout status deployment/<name> -n <namespace>

# 4. Tear down
kill $TUNNEL_PID
```

| Flag | Description |
| ---- | ----------- |
| `--port-forward LOCAL:REMOTE` | Forward a local port to the jumpbox |
| `--new` | Force creation of a new Bastion session |
| `--session-id OCID` | Reuse a specific session by OCID |

If an ACTIVE Bastion session already exists for the SSH key, the script reuses it.

> Infra scripts (LB wiring, etc.) live in a separate `devops-scripts` repo. Push changes there
> first, then `git pull` on the infra jumpbox before running them — there is no automatic sync.

GitHub pushes use whatever credential you normally use (SSH key or a PAT with `repo` scope).
The GitOps remediation feature also needs a PAT — see "GitOps-first remediation" below.

---

## Observability Stack — Grafana / Prometheus / Loki

### What's Deployed (Staging)

| Release | Namespace | Chart | K8s Ingress Class |
| ------- | --------- | ----- | ----------------- |
| `prom-stack` | `observability` | `kube-prometheus-stack` | `ingress-nginx` |
| `loki-stack` | `observability` | wrapper `monitoring-loki` (subchart `grafana/loki`, Loki 3.x) — deploy from `helm-charts/helmcharts/observability/loki`, never `grafana/loki` directly | `ingress-nginx` |
| `monitoring-grafana` | `observability` | `grafana/grafana` | `ingress-nginx` |
| `tempo` | `observability` | `grafana/tempo` | `ingress-nginx` |
| `otelcol` | `observability` | `open-telemetry/opentelemetry-collector` | `ingress-nginx` |

**Helm chart source:** `charts/observability/`
**Helmcharts mirror:** `helm-charts/helmcharts/observability/`
**Deploy workflow:** `Actions → Deploy Observability Stack`

**Grafana login:** `admin`, password stored in the `grafana-admin` Secret (`<grafana-admin-password>`).

---

### Ingress Controllers in the Staging Cluster (example)

| Class | Namespace | LB IP (private) |
| ----- | --------- | --------------- |
| `ingress-nginx` | `ingress-nginx` | `10.0.2.10` |
| `ai-apps` | `ai-apps` | `10.0.2.10` / `10.0.2.11` |
| `public-apps` | `public-apps` | `10.0.2.12` |
| `kong` | `kong` | `10.0.2.13` |

> The observability ingresses (`kubectl get ingress -n observability`) report ADDRESS `10.0.2.10`.
> All services use class `ingress-nginx` with port 80 (no TLS termination at ingress level).

---

### K8s Ingress URLs

| Host | Backend Service | Port | Namespace |
| ---- | --------------- | ---- | --------- |
| `grafana-stg.example.com` | `monitoring-grafana` | `80` | `observability` |
| `prometheus-stg.example.com` | `prom-stack-kube-prometheus-prometheus` | `9090` | `observability` |
| `loki-stg.example.com` | `loki-stack` | `3100` | `observability` |
| `tempo-stg.example.com` | `tempo` | `3200` | `observability` |
| `otel-stg.example.com` | `otelcol-opentelemetry-collector` | `4318` (OTLP HTTP) | `observability` |

---

### Public Access — OCI Load Balancer

**LB name:** `stg-public-lb`
**Public IP:** `203.0.113.10`
**OCID:** `ocid1.loadbalancer.oc1.us-ashburn-1.aaaaaaaaexampleloadbalancer`

**Traffic path:**
```
Browser → https://grafana-stg.example.com (203.0.113.10:443)
        → stg-public-lb  (stg-listener, HTTP2, SSL terminated)
        → routing policy: monitoring_stg_routing
        → backend set: bs_ingress_nginx_stg → 10.0.2.10:80
        → ingress-nginx controller (host-header routing)
        → K8s service in observability namespace
```

**Listener:** `stg-listener` — port 443, protocol HTTP2
**Default backend:** `bs_default` (→ `10.0.2.12:443`, public-apps ingress)
**SSL cert:** wildcard `*.example.com`, `ocid1.certificate.oc1.us-ashburn-1.aaaaaaaaexamplecertificate`

**Routing policy rules** (`monitoring_stg_routing`):
| Rule name | Condition | Backend set |
| --------- | --------- | ----------- |
| `route_to_stg_grafana` | `host == grafana-stg.example.com` | `bs_ingress_nginx_stg` |
| `route_to_stg_prometheus` | `host == prometheus-stg.example.com` | `bs_ingress_nginx_stg` |
| `route_to_stg_loki` | `host == loki-stg.example.com` | `bs_ingress_nginx_stg` |
| `route_to_stg_tempo` | `host == tempo-stg.example.com` | `bs_ingress_nginx_stg` |
| `route_to_stg_otel` | `host == otel-stg.example.com` | `bs_ingress_nginx_stg` |

**Backend set:** `bs_ingress_nginx_stg` → `10.0.2.10:80`

---

### LB Scripts (in `devops-scripts/oci/load-balancer/`)

| Script | Purpose |
| ------ | ------- |
| `add-monitoring-to-stg-lb.sh` | Wires the monitoring hosts into `stg-public-lb` (backend set + routing rules). Idempotent, safe to re-run. |
| `inspect-stg-lb.sh` | Read-only inspection of the LB (listeners, backends, routing, health) |

Run from the OCI infra jumpbox:
```bash
cd ~/devops-scripts
bash oci/load-balancer/inspect-stg-lb.sh            # check current state
bash oci/load-balancer/add-monitoring-to-stg-lb.sh  # wire monitoring (idempotent)
```

---

### DNS Records

Add A records for each host pointing at the public LB IP, e.g.:

```
grafana-stg.example.com.    300 IN A 203.0.113.10
prometheus-stg.example.com. 300 IN A 203.0.113.10
loki-stg.example.com.       300 IN A 203.0.113.10
tempo-stg.example.com.      300 IN A 203.0.113.10
otel-stg.example.com.       300 IN A 203.0.113.10
```

---

### Quick Browser Access (before DNS — kubectl port-forward)

```bash
bash ~/bastion/oke-jumpbox.sh --port-forward 6443:6443 &
bash scripts/open-monitoring.sh
```

Ports opened locally: Grafana → `3000`, Prometheus → `9090`, Loki → `3100`

---

### Lessons Learned — OCI LB API Quirks

| Problem | Root cause | Fix |
| ------- | ---------- | --- |
| Routing policy create fails silently | Missing `--condition-language-version "V1"` | Always pass this flag on `oci lb routing-policy create` |
| Backend set not visible after create | `wait_for_lb` polls lifecycle state which returns ACTIVE before OCI commits the backend | Use `wait_for_work_request` polling `oci lb work-request get` until SUCCEEDED |
| HTTP2 listener rejects cipher suite | HTTP2 listeners require `oci-default-http2-ssl-cipher-suite-v1`, not `oci-default-ssl-cipher-suite-v1` | Auto-discover cipher suite from existing listener config |
| 16-hostname-entry OCI limit | An LB can hit the 16 hostname-entry limit | Don't create hostname entries; use routing policy host-header conditions instead |
| `set -e` exits script silently | OCI CLI error swallowed | Log OCI CLI output to a file and check it |
| Backend health UNKNOWN after create | Health checker warm-up | Normal — wait ~30 s |

---

## OCI References (Staging, example values)

| Resource | Value |
| -------- | ----- |
| Region | `us-ashburn-1` |
| OKE cluster OCID | `ocid1.cluster.oc1.us-ashburn-1.aaaaaaaaexamplecluster` |
| Compartment | `ocid1.compartment.oc1..aaaaaaaaexample` |
| VCN | `stg-vcn` |
| stg-public-lb IP | `203.0.113.10` |
| prod-public-lb IP | `203.0.113.20` |

---

## Troubleshooting (access)

| Symptom | Fix |
| ------- | --- |
| `Connection refused` on port 6443 | Tunnel not ready yet — wait and retry |
| `Session entered terminal state: DELETING` | Run with `--new` to create a fresh session |
| SSH key error | Confirm `~/.ssh/bastion_key` and `.pub` exist |
| OCI auth failure | Re-run `oci session authenticate` |
| Script not found on jumpbox | Not pulled yet — push to GitHub, `git pull` on jumpbox |

---

## Diagnose Workflows

**Runner:** a self-hosted runner with direct cluster access (`stg-runners`, no bastion tunnel needed)
**Trigger:** GitHub Actions → workflow_dispatch

The diagnose workflows are split by namespace. Use the one that matches what you're debugging.

### Namespace split

| Component | Namespace | Workflow |
| --------- | --------- | -------- |
| Prometheus, Grafana, Loki, Tempo, OTel Collector (all Helm releases) | `observability` | `diagnose-observability.yml` |
| claude-agent CronJob, RBAC | `monitoring` | `diagnose-monitoring.yml` |

> **Critical:** Never mix namespaces — using the wrong namespace is a silent failure (pod not found, PVC not found).

---

### diagnose-monitoring.yml — claude-agent (monitoring ns)

| Input | Default | Purpose |
| ----- | ------- | ------- |
| `run_test_alert` | `no` | Send a synthetic TEST_ALERT Teams message — verifies the full notification pipeline. |
| `apply_rbac` | `no` | Apply `k8s/rbac.yaml` — ServiceAccount, Role, ClusterRole, bindings for claude-agent. |

---

### diagnose-observability.yml — observability stack (observability ns)

| Input | Default | Purpose |
| ----- | ------- | ------- |
| `fix_prometheus` | `no` | Force-delete stuck Prometheus pod so StatefulSet recreates it |
| `fix_prometheus_pvc` | `no` | Patch VA + PVC finalizers to null, delete PVC (`--wait=false`), force-delete pod. Fresh 50Gi volume. **Loses metrics data.** |
| `fix_volume_attachment` | `no` | Delete stale VolumeAttachment + force-delete pod. Use when Multi-Attach loop is blocking new pod. |
| `fix_stuck_va` | `no` | Patch VA finalizer to null, delete VA, patch PVC finalizer to null, delete PVC (`--wait=false`), force-delete pod. **Use for WAL death spiral.** |
| `fix_pause_and_recover` | `no` | Pause Prometheus CR (stops Operator reconciliation), force-delete pod, wait 120s, unpause. Use when Operator is the cause of restarts. |
| `fix_scale_operator` | `no` | Scale Prometheus Operator to 0, let pod recover 120s, scale back to 1. Use to detect Operator-driven rolling restarts. |
| `fix_rancher_operator_scope` | `no` | Patch `rancher-monitoring-operator` to add `--namespaces=cattle-monitoring-system`, stopping it from reconciling our Prometheus CR. |
| `run_test_trace` | `no` | Send a synthetic OTLP trace through OTel Collector → Tempo and verify storage. Confirms end-to-end tracing pipeline is working. |

### Known OCI block volume issues

| Symptom | Root cause | Fix |
| ------- | ---------- | --- |
| Prometheus pod stuck `Terminating` for days | OCI BV I/O error on node; pod can't unmount | `fix_prometheus_pvc=yes` to delete PVC and get a fresh volume |
| Multi-Attach errors on new pod | Stale `VolumeAttachment` object not cleaned after force-delete | `fix_volume_attachment=yes` to delete the VA, then pod attaches cleanly |
| `kubectl delete pvc` hangs in workflow | OCI CSI `external-attacher` finalizer on VolumeAttachment won't release; AND a new VA may be created between fix steps as StatefulSet immediately recreates the pod | Both `fix_stuck_va` and `fix_prometheus_pvc` steps use `--wait=false`. Run both inputs together — `fix_stuck_va` patches old VA first, `fix_prometheus_pvc` patches any new VA created between steps. |
| `NodeExpandVolume` RBAC error | OCI BV minimum is 50Gi; requesting 10Gi causes K8s to attempt resize on wrong node | Set `storageSpec.storage: 50Gi` in stg-values.yaml to match OCI minimum |
| Pod bounces between nodes (Multi-Attach loop) | Scheduler places RWO pod on different nodes each cycle | Add `affinity.nodeAffinity` to pin Prometheus to one node (e.g. `10.0.2.21`) |

### Rancher operator conflict

Applies when the cluster also runs Rancher Monitoring (`cattle-monitoring-system`).

| Symptom | Root cause | Fix |
| ------- | ---------- | --- |
| Prometheus pod cycling every ~2s; STS generation ~1/sec | Rancher's `rancher-monitoring-operator` (cattle-monitoring-system) has no `--namespaces` flag — reconciles our Prometheus CR; both operators share SSA field manager `PrometheusOperator`; `prometheus-operator-input-hash` oscillates between 3 values | `prometheusOperator.namespaces.releaseNamespace: true` in stg-values.yaml. Also removed `additionalServiceMonitors` entirely so no cross-namespace reference exists in our config hash — cattle-monitoring-system fully ignored. |
| node-exporter pods Pending permanently | Rancher's `rancher-monitoring-prometheus-node-exporter` DaemonSet already binds `hostPort: 9100` on all nodes | Set `nodeExporter.enabled: false` in stg-values.yaml. Rancher's node-exporter already covers the nodes. |

> **Important:** Do NOT add `additionalServiceMonitors` that reference `cattle-monitoring-system`. Any cross-namespace scrape reference from our Prometheus CR bakes `cattle-monitoring-system` resources into the config hash — if Rancher's operator recalculates differently, hash oscillation and the rolling-update loop return.

### Prometheus WAL death spiral

| Symptom | Root cause | Fix |
| ------- | ---------- | --- |
| Prometheus 503, pod `1/2 Running`, 30-40 restarts, startup probe 503 | Each failed startup generates new WAL segments. WAL replay at ~5s/segment grows until it exceeds startup probe timeout (~15 min) → pod killed → more WAL → death spiral. Can recur whenever pod restarts repeatedly (Rancher conflict, node eviction, etc.) | Run workflow with **both** `fix_stuck_va=yes` AND `fix_prometheus_pvc=yes` — patches VA/PVC finalizers, deletes PVC (`--wait=false`), force-deletes pod. Prometheus Operator provisions fresh 50Gi volume (empty WAL); pod starts in ~60s |
| `kubectl delete pvc` hangs indefinitely in workflow | `fix_stuck_va` force-deletes pod → StatefulSet immediately recreates pod → new VolumeAttachment is created with CSI finalizer → subsequent `kubectl delete pvc` without `--wait=false` blocks forever waiting for new VA to be cleaned up | Both fix steps use `kubectl delete pvc --wait=false` and patch the new VA's finalizer to null before attempting deletion. |

> **Fix order for WAL spiral:** run BOTH `fix_stuck_va=yes` AND `fix_prometheus_pvc=yes` in the same workflow dispatch. `fix_stuck_va` runs first (clears old VA + triggers PVC delete), `fix_prometheus_pvc` runs second (patches any new VA created between steps + triggers PVC delete again). Both use `--wait=false` so neither step can hang.
>
> **Do NOT use `fix_prometheus_pvc=yes` alone** without `fix_stuck_va=yes` — if the original VA still has a finalizer, the PVC delete will block.
>
> **Why it recurs:** the WAL spiral is a symptom of repeated pod restarts, not a one-time event. If the root cause (Rancher conflict, node issue) recurs, so does the WAL spiral. Fix the underlying restart cause after clearing WAL.

### Prometheus storageSpec (charts/observability/prometheus/values/stg-values.yaml)

- `storage: 50Gi` — matches OCI BV minimum (prevents NodeExpandVolume errors)
- `affinity.nodeAffinity` preferred on a single node — prevents Multi-Attach loop
- `prometheusOperator.namespaces.releaseNamespace: true` — restricts our operator to the observability namespace only
- `nodeExporter.enabled: false` — Rancher already provides node-exporter on all nodes
- **No `additionalServiceMonitors`** — cattle-monitoring-system fully ignored

### Helm charts mirror

Charts are mirrored to a separate `helm-charts` repo on every Deploy Observability Stack run.
The sync step uses secret `HELMCHARTS_PAT`.

---

## Loki 3.x Migration

Migrated from `grafana/loki-stack` (Loki 2.x) to `grafana/loki >=6.6.0` chart (Loki 3.x, single binary mode).

**Why:** Grafana 10 requires two Loki 3.x-only endpoints:
- `/loki/api/v1/status/buildinfo` — Grafana datasource Test button
- `/loki/api/v1/index/volume` — Log volume chart in Logs Drilldown

**Chart location:** `charts/observability/loki/`
**Chart version:** `grafana/loki >=6.6.0` + `grafana/promtail >=6.0.0`

### Key config choices

| Setting | Value | Reason |
| ------- | ----- | ------ |
| `fullnameOverride: loki-stack` | Preserves service DNS `loki-stack.observability:3100` | Grafana + Promtail URLs need no change |
| `deploymentMode: SingleBinary` | Single replica | Staging — no HA needed |
| `storage.type: filesystem` | Local PVC | No object storage in staging |
| `schemaConfig.store: tsdb, schema: v13` | Loki 3.x TSDB | Old BoltDB data on PVC ignored; fresh start |
| `limits_config.volume_enabled: true` | Enables `/index/volume` API | Required for Grafana Log volume chart |
| `chunksCache.enabled: false` | Disables Memcached | Images fail in a private cluster without registry access |
| `resultsCache.enabled: false` | Disables Memcached | Images fail in a private cluster without registry access |
| `lokiCanary.enabled: false` | Disables canary DaemonSet | DaemonSet image fails in a private cluster |
| `sidecar.rules.enabled: false` | Disables `kiwigrid/k8s-sidecar` container | Image fails in a private cluster |
| `monitoring.*: false` | Disables self-monitoring | Using our own Prometheus stack |

### StatefulSet pre-delete requirement

`grafana/loki >=6.6.0` changes label selectors from `app: loki` to `app.kubernetes.io/name: loki`.
Immutable `spec.selector` and `volumeClaimTemplates` fields cannot be patched — `helm upgrade --install` (even with `--force`) fails at the K8s API level.

**Fix:** `kubectl delete statefulset loki-stack -n observability --ignore-not-found=true` before `helm upgrade`.
The PVC (`storage-loki-stack-0`, 50Gi) is preserved; the new StatefulSet claims it automatically via the same `volumeClaimTemplate` name `storage` and the same StatefulSet name (from `fullnameOverride`).

### Label selector changed (affects kubectl)

| Loki 2.x | Loki 3.x |
| -------- | -------- |
| `app=loki` | `app.kubernetes.io/name=loki` |

Use `kubectl -n observability get pods -l "app.kubernetes.io/name=loki"` to inspect Loki pods.

### Loki outage — PVC full + CRI-O

`loki-stack-0` down: wedged CRI-O on one node, then `no space left on device`
(months of data — `limits_config.retention_period` alone deletes nothing with TSDB), then
CRI-O SELinux relabel of the large PVC exceeding the CRI timeout. Fixed state:

| Setting | Value |
| ------- | ----- |
| `loki.loki.compactor.retention_enabled` | `true` (+ `delete_request_store: filesystem`) — the only thing that actually deletes with TSDB |
| `loki.loki.podSecurityContext` | `seLinuxOptions.type: spc_t`, `fsGroupChangePolicy: OnRootMismatch` — skips the slow relabel |
| PVC `storage-loki-stack-0` | expanded online to **100Gi** (`oci-bv` allows expansion). Values keep `size: 50Gi` — STS `volumeClaimTemplates` are immutable |
| Memory limit | `1Gi` |

Deploy (wrapper chart):
```bash
cd helm-charts/helmcharts/observability/loki && helm dependency build
helm -n observability upgrade loki-stack . -f values.yaml -f values/stg-values.yaml
```
`helm upgrade ... grafana/loki --reuse-values` fails (`nil pointer .Values.monitoring.dashboards`) and
`--reset-then-reuse-values` flips to SimpleScalable (`Please define loki.storage.bucketNames.chunks`) — both
because the release is the wrapper chart.

---

## OpenTelemetry / Distributed Tracing

### Architecture

```
Application (OTLP SDK)
    │
    ▼  HTTP :4318  or  gRPC :4317
otelcol-opentelemetry-collector.observability
    │
    ▼  gRPC :4317
tempo.observability:3200
    │
    ▼
Grafana → Tempo datasource → Traces Drilldown / TraceQL
```

**OTel Collector is NOT a Grafana datasource** — it is a pipeline. Grafana talks directly to Tempo.

### Endpoints

| Service | In-cluster URL | Protocol |
| ------- | -------------- | -------- |
| OTel Collector (HTTP) | `http://otelcol-opentelemetry-collector.observability:4318` | OTLP/HTTP |
| OTel Collector (gRPC) | `otelcol-opentelemetry-collector.observability:4317` | OTLP/gRPC |
| Tempo query | `http://tempo.observability:3200` | HTTP |
| Tempo ingest (gRPC) | `tempo.observability:4317` | OTLP/gRPC |

**Public OTLP endpoint (example):** `https://otel-stg.example.com` (via public LB → ingress-nginx → otelcol service)

### Instrumenting an application

Traces are NOT collected automatically (unlike logs via Promtail). Each application must use an OTLP SDK.

**Python:**
```bash
pip install opentelemetry-sdk opentelemetry-exporter-otlp-proto-http
```
```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

provider = TracerProvider()
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
    endpoint="http://otelcol-opentelemetry-collector.observability:4318/v1/traces"
)))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("my-operation"):
    # your code here
```

**.NET:**
```bash
dotnet add package OpenTelemetry.Exporter.OpenTelemetryProtocol
```
```csharp
builder.Services.AddOpenTelemetry()
    .WithTracing(b => b
        .AddAspNetCoreInstrumentation()
        .AddOtlpExporter(o => o.Endpoint = new Uri(
            "http://otelcol-opentelemetry-collector.observability:4318")));
```

**Node.js:**
```bash
npm install @opentelemetry/sdk-node @opentelemetry/exporter-trace-otlp-http
```
```js
const { NodeSDK } = require('@opentelemetry/sdk-node');
const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-http');
const sdk = new NodeSDK({
  traceExporter: new OTLPTraceExporter({
    url: 'http://otelcol-opentelemetry-collector.observability:4318/v1/traces'
  })
});
sdk.start();
```

### Verifying the pipeline

Run **Diagnose Observability Workflow** (`diagnose-observability.yml`) with `run_test_trace=yes`. This sends a synthetic OTLP trace and verifies it is stored in Tempo. Output includes the trace ID and direct Grafana search URL.

### Tempo metrics-generator

Enabled to power TraceQL `| rate()` streaming aggregation in Grafana Traces Drilldown.

Key config in `charts/observability/tempo/values.yaml`:
```yaml
tempo:
  tempo:
    metricsGenerator:
      enabled: true
      remoteWriteUrl: "http://prom-stack-kube-prometheus-prometheus.observability:9090/api/v1/write"
```

**Important:** `metricsGenerator` is camelCase (grafana/tempo chart's Helm values field). Do NOT use `metrics_generator:` (raw Tempo config) — the chart silently ignores it, leaving `empty ring` errors in Traces Drilldown.

Prometheus must have `enableRemoteWriteReceiver: true` (already set in `prometheus/values/stg-values.yaml`).

### ConfigMap changes require manual pod restart

Neither grafana/tempo nor grafana/loki charts embed config checksums in pod templates.
After `helm upgrade`, always run:
```bash
kubectl rollout restart statefulset/tempo -n observability
kubectl rollout restart statefulset/loki-stack -n observability
```
The Deploy Observability Stack workflow does this automatically for Tempo.

---

## Claude Agent

**CronJob:** `monitoring/claude-agent` — runs every 5 minutes, `backoffLimit: 0`
**Image:** `registry.example.com/platform/claude-agent:main-<short-sha>`
**Build workflow:** `Build & Deploy` (`.github/workflows/deploy.yml`)

### Health detection — Pods (`agent/health_checker.py`)

| Detection | Mechanism | Threshold |
| --------- | --------- | --------- |
| `CrashLoopBackOff`, `ImagePullBackOff`, `ErrImagePull`, `OOMKilled`, `Error`, `CreateContainerConfigError`, `InvalidImageName`, `RunContainerError` | `containerStatuses[].state.waiting.reason` | immediate |
| `PostStartHookError`, `CreateContainerError`, `ImageInspectError`, `RegistryUnavailable`, `ErrImageNeverPull` | `containerStatuses[].state.waiting.reason` | immediate |
| **Evicted** | `phase==Failed AND status.reason=="Evicted"` | immediate |
| **Pending stuck** — Unschedulable or generic | `phase==Pending` + age + `PodScheduled=False` condition | > 2 min |
| **Terminating stuck** | `deletionTimestamp` set + age | > 2 min |
| **ContainerCreating stuck** | `waiting.reason==ContainerCreating` + age | > 2 min |
| **ContainersReady=False** | `conditions[].type==ContainersReady, status==False` | Running pods |
| **Initialized=False** | `conditions[].type==Initialized, status==False` | Running pods |
| **High restart count — warning** | `containerStatuses[].restartCount` | ≥ 5 |
| **High restart count — critical** | `containerStatuses[].restartCount` | ≥ 20 (death spiral) |
| Waiting `message` in issue text | `state.waiting.message` (first 160 chars) appended, e.g. `CreateContainerError (context deadline exceeded)` | immediate |
| **Node runtime (CRI-O) wedged** | If this workload's pod is stuck creating (`CreateContainerError`, or `ContainerCreating` > 2 min), list every pod on that node (`spec.nodeName`); ≥ 2 stuck across workloads → node issue, not workload. Cached per node per cycle | ≥ 2 pods |

### Health detection — Storage (`agent/storage_checker.py`)

Per PVC mounted by the workload's pods. Prometheus (`kubelet_volume_stats_*`) is queried **once per cycle,
cluster-wide**, with `max by (namespace, persistentvolumeclaim)` — on clusters where kubelet is scraped twice
(e.g. `rancher-monitoring-kubelet` + `prom-stack-kube-prometheus-kubelet`), raw series are duplicated.

| Detection | Mechanism | Threshold |
| --------- | --------- | --------- |
| PVC missing | `read_namespaced_persistent_volume_claim` 404 | immediate |
| PVC not Bound / Lost | `status.phase` (+ `WaitForFirstConsumer` hint from the SC) | Pending > 2 min, Lost immediate |
| StorageClass missing / no default SC | `list_storage_class` vs `spec.storageClassName` | immediate |
| Resize state | `status.conditions`: `Resizing`, `FileSystemResizePending`, `ControllerResizeError`, `NodeResizeError`; or requested > capacity | immediate |
| **PVC usage** | `kubelet_volume_stats_used_bytes / capacity_bytes` — issue includes the exact `kubectl patch pvc` command when the SC has `allowVolumeExpansion: true` | ≥ 80% WARNING, ≥ 90% CRITICAL |
| Inode exhaustion | `kubelet_volume_stats_inodes_used / inodes` | ≥ 90% |

RBAC: `storageclasses` (storage.k8s.io) + `persistentvolumes` get/list/watch in `k8s/rbac.yaml`.
Open follow-ups (cluster-wide PVC sweep, predictive fill, `expand_pvc` remediation): `docs/open-items.md` OI-1.

### Health detection — Services

| Detection | Mechanism |
| --------- | --------- |
| Ready replicas < desired | Deployment status |
| K8s Service endpoint = 0 (readiness probe) | `Endpoints.subsets[].addresses` count |
| K8s Service selector matches 0 pods | 0 addresses AND selector is set |
| Empty selector (manual endpoints) | `spec.selector == {}` |
| LoadBalancer no external IP > 2 min | `spec.type==LoadBalancer` + age |
| targetPort mismatch | Cross-references `spec.ports[].targetPort` against pod `containerPorts` |

### Health detection — Ingress

| Detection | Mechanism |
| --------- | --------- |
| No `ingressClassName` and no annotation | Missing field check |
| IngressClass not found in cluster | Lists all `IngressClass` resources, compares |
| TLS secret missing (404) | `read_namespaced_secret` per `spec.tls[].secretName` |
| TLS secret wrong type | `secret.type != kubernetes.io/tls` |
| Duplicate hostname | Cluster-wide hostname map built in `discover_stage_ingresses` |
| Backend service not found (404) | Existing behaviour + similar-name hint |
| Backend port not in Service spec | Compares ingress backend port against `svc.spec.ports` |
| Backend 0 ready endpoints | Endpoint address count |
| No LB address > 3 min | `status.loadBalancer.ingress` empty + age |
| No rules and no defaultBackend | `spec.rules == [] AND spec.defaultBackend == null` |

### Shared observability HTTP clients (`agent/observability_clients.py`)

Thin Prometheus/Loki/Tempo query functions shared between `ClaudeAnalyzer`'s on-demand tools and
`ContextCollector`'s correlated-timeline pre-fetch — added when the same query logic was needed
in both places. Every function returns `(data, error)`; `error is None` on success (even if
`data` is empty). Do not re-add raw `requests.get(...)` calls to Prometheus/Loki/Tempo elsewhere —
add a function here instead.

### Correlated timeline (`agent/context_collector.py::build_correlated_timeline`)

Identity-aware correlation, not just timestamp overlap: when `TEMPO_URL` is set, pre-fetches
recent Tempo error traces for the service, then queries Loki filtered on each trace ID
(`|= "<traceID>"`) and Prometheus (CPU rate) for the same ~30 min window. Feeds a
`=== CORRELATED TIMELINE ===` prompt section. Degrades gracefully at every step — no traces, no
Tempo/Loki/Prometheus configured, or app logs not actually carrying trace IDs, all just produce
an empty/partial timeline, never an exception. Bounded to the top 5 traces to cap the number of
Loki queries per incident.

### Prometheus queries (`agent/claude_analyzer.py` — live `query_prometheus` tool)

Not pre-fetched — Claude calls these live mid-investigation via the agentic tool-use loop
(`analyze_with_claude`'s `TOOLS`). Known-good metric names, listed in the tool's description so
Claude doesn't have to guess:

| Metric | Purpose |
| ------ | ------- |
| `kube_pod_container_status_waiting_reason` | All current container waiting reasons |
| `kube_pod_status_unschedulable` | Unschedulable pods |
| `kube_pod_status_reason{reason="Evicted"}` | Evicted pods |
| `kube_pod_status_ready{condition="false"}` | Not-ready pods |
| `kube_persistentvolumeclaim_status_phase{phase!="Bound"}` | Unbound PVCs (block pod scheduling) |
| `kube_node_status_condition{condition=~"DiskPressure\|MemoryPressure\|PIDPressure"}` | Node pressure (causes evictions) |
| `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` | OOMKilled containers |
| `kube_pod_container_status_restarts_total` | Restart counts |
| `container_memory_working_set_bytes` | Memory working set per pod |
| `container_cpu_usage_seconds_total` | CPU rate per pod |

### Pre-pass checklist (`agent/prepass.py`)

Cheap, deterministic checks run over data already collected (zero new API calls) before the
Claude call, feeding advisory hints into the prompt. Claude is told to verify these, not restate
them uncritically.

| Check | Signal | Notes |
| ----- | ------ | ----- |
| OOMKilled | `pod_statuses[*].containers[*].reason` or `last_reason == "OOMKilled"` | `last_reason` (from `cs.last_state.terminated.reason`) catches OOM even after the container has already restarted into `CrashLoopBackOff` |
| Evicted | `"Evicted"` substring in `health_report["issues"]` | Node resource pressure, not an app bug |
| Recent rollout | `events` with `reason == "ScalingReplicaSet"` within the last 10 min | Deployments only — StatefulSets don't emit a single clean rollout event |
| Known signatures | Regex over `issues` + `loki_logs` | `martin` `--auto-publish` invalid flag; Redis `cluster_state:fail`/`CLUSTERDOWN`. Scoped to app services this agent monitors — does NOT include the Rancher-operator-conflict/WAL-death-spiral signatures above, which are specific to the Prometheus StatefulSet in `observability` (out of this agent's `NAMESPACES` scope, handled by `diagnose-observability.yml` instead) |
| PVC full / inodes / resize / unbound | `health_report["storage"]` from `storage_checker.py` | ≥ 90% full → "very likely the root cause" + expand path (or "SC not expandable"); `FileSystemResizePending` → delete pod; never change STS volumeClaimTemplate in Helm |
| CreateContainerError timeout | container `message` matches `context deadline exceeded` / `name is reserved` / `RST_STREAM` | Splits into: node-runtime issue present → "CRI-O wedged, fix the node"; PVC mounted → "SELinux relabel, set `spc_t` + `OnRootMismatch`"; else → check CRI-O |
| Signatures `disk-full`, `crio-name-reserved` | `no space left on device` / `ENOSPC`; `name is reserved` | Signature search covers issues, Loki logs, **kubectl-logs fallback**, event messages and container `message`/`last_message` |

### Outcome store — remediation grounding (`agent/outcome_store.py`)

Minimal viable RAG: after a mutating remediation command's post-check (service/deployment path
only — `agent/monitor.py`'s `handle_reply`), the outcome (service, namespace, hypothesis,
command, resolved, time-to-resolve) is persisted to a ConfigMap `claude-agent-outcomes` in the
agent's own namespace (`POD_NAMESPACE`, same namespace/pattern as the `claude-agent-cooldown`
ConfigMap). Bounded to the most recent 50 records (oldest dropped first).

On the next incident for that same service, `ClaudeAnalyzer._build_prompt` retrieves the most
recent matches (exact service+namespace match, no embeddings) and injects them into the prompt
as a `=== PAST INCIDENTS ===` section — Claude is told to prefer a command already recorded as
RESOLVED for matching symptoms, and be skeptical of one already recorded as not resolving it.

**RBAC:** the `claude-agent-secret-writer` Role's `configmaps` get/patch rule (`k8s/rbac.yaml`)
covers both `claude-agent-cooldown` and `claude-agent-outcomes` by resourceName.

Ingress remediation does not persist outcomes — `handle_ingress_reply` has no post-check to
source a `resolved` signal from (unlike the service/deployment path), so there's nothing
meaningful to record there yet.

### GitOps-first remediation for Helm-owned drift (`agent/gitops_remediation.py`)

The recurring problem: a live `kubectl patch ingress` fix for a duplicate-hostname
conflict gets reverted on the next Helm/GitOps reconcile, so the alert just fires again every
cooldown window (`agent/monitor.py`'s ingress cooldown comment: "Root cause likely requires a
Helm chart values fix"). Instead of only patching the live object, the agent can open a PR
against the owning chart's values file — HolmesGPT's "Operator Mode" pattern.

**Opt-in only** — set `GITOPS_REPO_MAP` (a JSON env var) mapping `"<namespace>/<ingress-name>"` to
`{"repo": "owner/repo", "path": "values.yaml", "yaml_path": "ingress.hosts[0].host", "base_branch": "main"}`.
With no entry for a given ingress this does nothing — there's no way to guess which repo/chart
owns an arbitrary monitored service's ingress, so nothing is inferred.

When a mapping exists and Claude's analysis includes a `kubectl patch ingress ... /spec/rules/N/host`
command, `agent/monitor.py`'s ingress loop calls `open_hostname_pr()` (needs `GITHUB_TOKEN`, a
classic PAT with `repo` scope) instead of just alerting: reads the current values file, sets the
target YAML path via a small dotted-path navigator, opens a branch + PR. Idempotent — checks for an
already-open PR on the deterministic branch name
(`claude-agent/fix-ingress-hostname-<namespace>-<name>`) before creating a new one, so repeated
cooldown cycles don't spam duplicate PRs. The Teams card shows the PR link instead of the raw
`kubectl patch` command when a PR was opened (a human still approves/merges the PR — this doesn't
skip review, it moves the fix to the right layer).

### Log fallback when Loki is the problem (`agent/context_collector.py`)

`kubectl logs` (current, then `--previous`) is used for unhealthy pods when Loki is unconfigured **or**
returns nothing / errors — the `"(no logs from Loki …)"` placeholder is cleared so the prompt uses the
fallback. Added because when Loki itself is the failing service, the only copy of
`no space left on device` is in `kubectl logs`. PVC events are fetched for the PVCs the pods actually
mount (`health_report["storage"]`), not only name matches.

### Known issues — resolved

| Symptom | Root cause | Fix |
| ------- | ---------- | --- |
| All pods `Error` / `BackoffLimitExceeded` | `agent/context_collector.py` — a string literal split across bare lines (`return "..." \n f"..."`) caused `IndentationError` at import time | Collapsed to a single f-string and rebuilt the image |
