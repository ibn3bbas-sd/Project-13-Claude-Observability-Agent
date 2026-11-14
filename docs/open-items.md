# Open Items — Claude Observability Agent

Detection gaps found in real incidents. Each item: what happened, what the agent missed, what was
built, and what is still open.

---

## OI-1 — Storage (PVC / StorageClass) and node-runtime blind spots

**Opened:** 2026-11-03 · **Status:** detection implemented, follow-ups open
**Runbook:** see CLAUDE.md → "Loki outage — PVC full + CRI-O"

### Incident

`observability/loki-stack-0` down on stg OKE. Three stacked causes:

| # | Real cause | What the agent reported | Why it missed the cause |
|---|---|---|---|
| 1 | CRI-O wedged on node `10.0.2.22` (`name is reserved`, runc `container does not exist`; `rabbitmq` stuck `ContainerCreating` 14h on the same node) | `Container loki: CreateContainerError` | Each pod evaluated alone — no view of "many pods on one node stuck" |
| 2 | Loki PVC 100% full — `no space left on device` (retention never ran: TSDB needs `compactor.retention_enabled`) | `CrashLoopBackOff` / restarts | No PVC usage check at all (`kubelet_volume_stats_*` never queried); Loki was the failing service, so the Loki log query returned nothing and the `kubectl logs` fallback never ran (`"(no logs…)"` string is truthy) |
| 3 | CRI-O recursive SELinux relabel of the large volume > 2-min CRI timeout | `CreateContainerError` again | `waiting.message` (`context deadline exceeded`) not used; no hint linking timeout + PVC |

### Built (this PR)

| Area | Change |
|---|---|
| `agent/storage_checker.py` (new) | Per PVC mounted by the workload's pods: missing (404), not `Bound` / `Lost`, StorageClass missing / no default SC, resize conditions (`Resizing`, `FileSystemResizePending`, `ControllerResizeError`, `NodeResizeError`), requested > capacity, usage via Prometheus `kubelet_volume_stats_*` (bytes **and** inodes): ≥80% WARNING, ≥90% CRITICAL, with the exact `kubectl patch pvc` command when the SC has `allowVolumeExpansion: true`. Prometheus queried once per cycle cluster-wide, cached |
| `agent/health_checker.py` | Calls the storage checker; new node-runtime check — if this workload's pod is stuck creating, lists all pods on that node; ≥2 stuck → "CRI-O likely wedged" issue. Container `waiting.message` now included in the issue text; `last_message` + pod `node` recorded |
| `agent/prepass.py` | Hints: PVC ≥90% full (expand path vs. non-expandable SC, "don't change the STS volumeClaimTemplate in Helm"), inode exhaustion, `FileSystemResizePending`, unbound PVC, `CreateContainerError` timeout → wedged node vs. SELinux relabel (`spc_t` + `OnRootMismatch`). Signatures `disk-full` and `crio-name-reserved`. Signature search now also covers kubectl-logs fallback, event messages and container messages |
| `agent/context_collector.py` | `kubectl logs` fallback also when Loki errors / returns nothing; PVC events fetched for the PVCs actually mounted, not only name matches |
| `k8s/rbac.yaml` | `storageclasses` (storage.k8s.io) + `persistentvolumes` get/list/watch |

### Still open

- [ ] **Verify `kubelet_volume_stats_*` exist in our Prometheus.** If the kubelet ServiceMonitor is off, usage checks silently skip. Check: `kubelet_volume_stats_used_bytes{persistentvolumeclaim="storage-loki-stack-0"}` in Grafana Explore
- [ ] **Unit tests** for `storage_checker.py` with mocked API/Prometheus (prepass covered by a smoke test only)
- [ ] **Cluster-wide PVC sweep** — today only PVCs of monitored workloads in `NAMESPACES` are checked. A full PVC in an unmonitored namespace is invisible
- [ ] **Remediation via policy registry** — add `expand_pvc` as tier 2 (dry-run + Teams approval), never auto; requires RBAC `persistentvolumeclaims: patch`
- [ ] **Node-runtime remediation** — no automated fix (needs node access); consider a Teams card linking the runbook commands
- [ ] **Predictive fill** — `predict_linear(kubelet_volume_stats_available_bytes[6h], 24*3600) < 0` to alert before 80%
- [ ] **Alertmanager overlap** — kube-prometheus-stack already ships `KubePersistentVolumeFillingUp`; confirm whether Alertmanager routes it anywhere, avoid double-alerting
- [ ] Apply the Loki retention + SELinux values (CLAUDE.md → Loki outage) to `charts/observability/loki` so a redeploy does not regress
