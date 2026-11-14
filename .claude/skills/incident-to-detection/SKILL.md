---
name: incident-to-detection
description: Turn a real cluster incident the claude-agent missed (or only half-diagnosed) into a new detection — gap analysis, checker module, pre-pass hint/signature, RBAC, open-items entry, mocked verification, deploy and live check. Use when the user shares an incident (kubectl output, logs) and asks to make the agent detect it next time, or to work an item in docs/open-items.md.
---

# From incident to agent detection

Captures the workflow used for OI-1 (the `loki-stack-0` outage that produced
`agent/storage_checker.py`). Reuse it for the next "the agent should have caught
this" request.

## 1. Gap analysis first — per cause, not per symptom

Incidents here are usually **stacked causes** (OI-1 had three: wedged CRI-O, full
PVC, SELinux relabel timeout). For each real cause, write one row:

| Real cause | What the agent reported | Why it missed it |

Read the actual detection code before deciding the "why": `agent/health_checker.py`
(`_evaluate_pod`, `check_service_health`), `agent/prepass.py`,
`agent/context_collector.py`. Typical misses found so far:

- signal never collected (no PVC usage query at all)
- signal collected but dropped (`waiting.message` captured, never used)
- per-pod view hides a node-level cause (one pod stuck vs. many on one node)
- **self-dependency**: the failing component is one the agent relies on. Loki
  down → Loki log query returns `"(no logs …)"` (truthy) → `kubectl logs`
  fallback never ran → `no space left on device` invisible. Always ask "what if
  the thing that broke is Loki/Prometheus/Tempo itself?"

## 2. Where each kind of fix goes

| Fix type | Home | Rule |
|---|---|---|
| New cluster signal (factual, alert-worthy) | new `agent/<area>_checker.py`, called from `check_service_health` | Appends to `report["issues"]`, sets `healthy=False`, stores structured status on the report (e.g. `report["storage"]`) for the pre-pass |
| Interpretation / likely root cause / fix path | `agent/prepass.py` — a `_check_*` function or a `KNOWN_SIGNATURES` entry | Hints are advice to Claude, not alerts. Keep issues factual, put "probably X, fix Y" here |
| Missing context (logs/events) | `agent/context_collector.py` | Fallbacks must also trigger when the preferred source is empty/erroring, not only when unconfigured |
| New API reads | `k8s/rbac.yaml` in the **same** commit | Cluster-scoped kinds (storageclasses, persistentvolumes, nodes) need the ClusterRole |

No changes to `claude_analyzer.py` / `monitor.py` / Teams card were needed for
OI-1 — new findings flow through `issues` + pre-pass hints. Prefer that path.

## 3. Checker module conventions (see `agent/storage_checker.py`)

- **Query Prometheus once per cycle, cluster-wide, cached** — never one query per
  object. Reset caches in `discover_stage_services()` (start of cycle).
- **Dedupe series with `max by (...)`**: this cluster scrapes kubelet twice
  (`rancher-monitoring-kubelet` and `prom-stack-kube-prometheus-kubelet` services),
  so every kubelet metric has duplicate series.
- Every check degrades silently-but-loggably: no `PROMETHEUS_URL`, missing
  metric, `ApiException` → skip, never raise. Wrap the call site in
  `check_service_health` with `try/except Exception` + `logger.warning`.
- Issue text = what is wrong + the exact command to fix it when safe to state
  (e.g. `kubectl patch pvc ...` only if the StorageClass `allowVolumeExpansion`).
- OCI specifics worth encoding: block volume minimum 50Gi; StatefulSet
  `volumeClaimTemplates` are immutable (never tell the user to change the size in
  Helm values); `FileSystemResizePending` needs a pod delete.

## 4. Verify — mocked, no cluster

```bash
cd agent && python3 -m py_compile *.py
```

- Pre-pass is pure: build a dict shaped like the real incident (copy the exact
  `waiting.message`, log line, usage %) and assert every root cause produces a hint.
- Checkers: patch `client.StorageV1Api` and `obs.prometheus_query` with
  `unittest.mock.patch.object`, feed `SimpleNamespace` objects for PVC/SC/pods.
  If importing the module outside the package fails on `observability_clients`,
  put a one-line stub on `PYTHONPATH` rather than editing imports.
- Run the incident-shaped test **before** pushing; quote the hints in the PR.

## 5. Docs in the same PR

- `docs/open-items.md`: one `OI-N` section — incident table, "Built" table,
  "Still open" checklist (unverified assumptions go here, e.g. "verify metric X
  exists").
- `CLAUDE.md` → "Claude Agent": add/extend the detection tables.
- Link the ops runbook if one was written.

## 6. Deploy and live-check (user runs kubectl — never run it yourself)

Hand the user these, in order:

```bash
# metric exists? (prometheus image has no wget/curl — use the API proxy)
kubectl get --raw '/api/v1/namespaces/observability/services/prom-stack-kube-prometheus-prometheus:9090/proxy/api/v1/query?query=<urlencoded-promql>'

# RBAC first (safe: read-only additions, old image ignores them)
cd ~/src/claude-observability-agent && git pull && kubectl apply -f k8s/rbac.yaml
kubectl auth can-i list storageclasses --as=system:serviceaccount:monitoring:claude-agent

# build+deploy — deploy.yml only builds from main, so merge first
gh workflow run deploy.yml --repo ibn3bbas-sd/Project-13-Claude-Observability-Agent --ref main
gh run watch --repo ibn3bbas-sd/Project-13-Claude-Observability-Agent

# run once now instead of waiting for the */5 cron
JOB=claude-agent-test-$(date +%s)
kubectl -n monitoring create job $JOB --from=cronjob/claude-agent
kubectl -n monitoring wait --for=condition=complete job/$JOB --timeout=300s
kubectl -n monitoring logs job/$JOB | grep -iE 'traceback|error|<new-check-keyword>'
```

Healthy checks log nothing — an empty grep on a healthy cluster is the pass
condition (plus job `complete`, no `Traceback`).
