"""
Collects rich context for an unhealthy service:
  - Kubernetes events (always)
  - Loki logs: last N lines from all pods (when LOKI_URL is set)
  - kubectl logs fallback: direct pod logs when Loki is not configured, OR when
    Loki returned nothing / errored (Loki may itself be the failing service —
    2026-11-03 incident: Loki's own PVC was full, so the 'no space left on
    device' line was only visible via kubectl logs)
  - Correlated timeline: recent Tempo error traces -> matching Loki log lines
    (by trace ID) -> a Prometheus range query for the same window, when
    TEMPO_URL is set (identity-aware correlation, not just timestamp overlap)

Claude can also query Prometheus/Loki/Tempo live via tools mid-investigation
for anything beyond this pre-fetch (see agent/claude_analyzer.py).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from kubernetes import client
from kubernetes.client.exceptions import ApiException

try:
    from .config import Config
    from . import observability_clients as obs
except ImportError:
    from config import Config
    import observability_clients as obs

logger = logging.getLogger(__name__)

_CORRELATION_WINDOW_MINUTES = 30
_MAX_CORRELATED_TRACES = 5


class ContextCollector:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.core_v1 = client.CoreV1Api()
        self.apps_v1 = client.AppsV1Api()

    def collect_context(
        self, svc: Dict[str, Any], health_report: Dict[str, Any]
    ) -> Dict[str, Any]:
        ns = svc["namespace"]
        name = svc["name"]

        context: Dict[str, Any] = {
            "service_name": name,
            "namespace": ns,
            "labels": svc.get("labels", {}),
            "health_report": health_report,
            "events": [],
            "pod_logs": {},
            "loki_logs": "",
            "deployment_spec": {},
            "correlated_timeline": {},
        }

        # 1. K8s events (always available)
        context["events"] = self._get_events(ns, name, svc, health_report)

        # 2. Loki logs — preferred; falls back to kubectl logs when Loki is not
        #    configured OR gave us nothing usable (down, erroring, or Loki is the
        #    failing service itself). The prompt uses pod_logs only when
        #    loki_logs is empty, so clear the "(no logs ...)" placeholder.
        if self.cfg.loki_url:
            context["loki_logs"] = self._query_loki_logs(ns, name)
            logger.info("Collected Loki logs for %s/%s", ns, name)
        loki_empty = not context["loki_logs"] or context["loki_logs"].startswith("(no logs")
        if loki_empty:
            context["loki_logs"] = ""
            for pod_status in health_report.get("pod_statuses", []):
                if not pod_status.get("healthy", True):
                    pod_name = pod_status["name"]
                    context["pod_logs"][pod_name] = self._get_pod_logs(ns, pod_name)
            if self.cfg.loki_url:
                logger.info("Loki returned no logs for %s/%s — used kubectl logs fallback", ns, name)

        # 3. Deployment spec summary
        context["deployment_spec"] = self._get_deployment_spec(ns, name)

        # 4. Identity-aware correlated timeline (Tempo traces -> matching logs -> metrics)
        if self.cfg.tempo_url:
            context["correlated_timeline"] = self.build_correlated_timeline(ns, name)

        return context

    # ------------------------------------------------------------------
    # Correlated timeline (Tempo -> Loki by trace ID -> Prometheus)
    # ------------------------------------------------------------------
    def build_correlated_timeline(self, ns: str, name: str) -> Dict[str, Any]:
        """
        Identity-aware correlation: recent Tempo error traces for this service ->
        trace IDs -> Loki log lines carrying those trace IDs -> a Prometheus range
        query for the same pod/window. One coherent timeline instead of three
        disjoint blobs.

        Degrades gracefully at every step: no Tempo config, no error traces, no
        Loki/Prometheus config, or app logs not actually carrying trace IDs (an
        unverified assumption for this cluster — see
        docs/ai-agent-enhancement-roadmap.md P1 item 4) all just produce an
        empty/partial timeline, never an exception.
        """
        timeline: Dict[str, Any] = {"traces": [], "correlated_logs": {}, "metric_series": {}}

        traces, error = obs.tempo_search_error_traces(
            self.cfg.tempo_url, name, minutes=_CORRELATION_WINDOW_MINUTES
        )
        if error:
            logger.debug("Correlated timeline: Tempo search failed for %s/%s: %s", ns, name, error)
            return timeline
        if not traces:
            return timeline

        top_traces = traces[:_MAX_CORRELATED_TRACES]
        timeline["traces"] = [
            {
                "traceID": t.get("traceID"),
                "rootService": t.get("rootServiceName"),
                "rootName": t.get("rootTraceName"),
                "durationMs": t.get("durationMs"),
            }
            for t in top_traces
        ]

        if self.cfg.loki_url:
            for t in top_traces:
                trace_id = t.get("traceID")
                if not trace_id:
                    continue
                logql = f'{{namespace="{ns}",pod=~"{name}-.*"}} |= "{trace_id}"'
                streams, log_error = obs.loki_query_range(
                    self.cfg.loki_url, logql, _CORRELATION_WINDOW_MINUTES, limit=20
                )
                if log_error:
                    logger.debug(
                        "Correlated timeline: Loki query failed for trace %s: %s", trace_id, log_error
                    )
                    continue
                lines = obs.loki_lines(streams, limit=20)
                if lines:
                    timeline["correlated_logs"][trace_id] = lines

        if self.cfg.prometheus_url:
            pod_re = f"{name}-.*"
            promql = (
                f'sum by (pod) (rate(container_cpu_usage_seconds_total'
                f'{{namespace="{ns}",pod=~"{pod_re}",container!="",container!="POD"}}[2m]))'
            )
            result, prom_error = obs.prometheus_query_range(
                self.cfg.prometheus_url, promql, _CORRELATION_WINDOW_MINUTES
            )
            if prom_error:
                logger.debug(
                    "Correlated timeline: Prometheus query failed for %s/%s: %s", ns, name, prom_error
                )
            elif result:
                timeline["metric_series"]["cpu_cores"] = result

        return timeline

    # ------------------------------------------------------------------
    # Loki
    # ------------------------------------------------------------------
    def _query_loki_logs(self, ns: str, name: str) -> str:
        logql = (
            f'{{namespace="{ns}",pod=~"{name}-.*"}}'
            ' |~ "(?i)(error|fatal|exception|critical|panic|traceback)"'
        )
        streams, error = obs.loki_query_range(
            self.cfg.loki_url, logql, self.cfg.loki_lookback_hours * 60, limit=50
        )
        if error:
            logger.warning("Loki query error for %s/%s: %s", ns, name, error)
            return ""

        lines: List[str] = []
        for stream in streams:
            pod = stream.get("stream", {}).get("pod", "unknown")
            for ts_ns, log_line in stream.get("values", []):
                ts = datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc)
                lines.append(f"[{ts.strftime('%H:%M:%S')}][{pod}] {log_line}")

        if not lines:
            return f"(no logs from Loki in the last {self.cfg.loki_lookback_hours}h)"

        # Sort chronologically (Loki returns newest-first when direction=backward)
        lines.reverse()
        return "\n".join(lines[-self.cfg.loki_max_lines:])

    # ------------------------------------------------------------------
    # K8s events
    # ------------------------------------------------------------------
    def _get_events(
        self,
        ns: str,
        dep_name: str,
        svc: Optional[Dict[str, Any]] = None,
        health_report: Optional[Dict[str, Any]] = None,
    ) -> List[Dict]:
        """Collect events for: the workload, unhealthy pods, related ReplicaSets, and PVCs."""
        seen: set = set()
        events: List[Dict] = []

        def _add(ev: Any) -> None:
            key = (ev.reason, ev.message, ev.involved_object.name)
            if key in seen:
                return
            seen.add(key)
            events.append({
                "type": ev.type,
                "reason": ev.reason,
                "message": ev.message,
                "count": ev.count,
                "first_time": ev.first_timestamp.isoformat() if ev.first_timestamp else None,
                "last_time": ev.last_timestamp.isoformat() if ev.last_timestamp else None,
                "involved_object": ev.involved_object.name,
                "involved_kind": ev.involved_object.kind,
            })

        def _fetch_by_name(obj_name: str) -> None:
            try:
                raw = self.core_v1.list_namespaced_event(
                    namespace=ns,
                    field_selector=f"involvedObject.name={obj_name}",
                )
                for ev in raw.items:
                    _add(ev)
            except ApiException as exc:
                logger.debug("Events fetch skipped for %s/%s: %s", ns, obj_name, exc.reason)

        # 1. Workload itself (Deployment / StatefulSet name)
        _fetch_by_name(dep_name)

        # 2. Unhealthy pods from health_report
        if health_report:
            for ps in health_report.get("pod_statuses", []):
                if not ps.get("healthy", True):
                    _fetch_by_name(ps["name"])

        # 3. Parent Deployment or StatefulSet (K8s object stored on svc dict)
        if svc:
            parent_obj = svc.get("_statefulset") or svc.get("_deployment")
            if parent_obj and hasattr(parent_obj, "metadata"):
                parent_name = parent_obj.metadata.name
                if parent_name != dep_name:
                    _fetch_by_name(parent_name)

            # 4. Related ReplicaSets (list RS whose name starts with dep_name-)
            try:
                rs_list = self.apps_v1.list_namespaced_replica_set(namespace=ns)
                for rs in rs_list.items:
                    rs_name = rs.metadata.name or ""
                    if rs_name.startswith(f"{dep_name}-"):
                        _fetch_by_name(rs_name)
            except ApiException as exc:
                logger.debug("Could not list ReplicaSets in %s: %s", ns, exc.reason)

            # 5. PVCs related to the workload — by name match AND by the PVCs the
            #    health check found mounted (resize/provisioning events live here)
            pvc_names = {
                st.get("pvc") for st in ((health_report or {}).get("storage") or []) if st.get("pvc")
            }
            try:
                pvcs = self.core_v1.list_namespaced_persistent_volume_claim(namespace=ns)
                for pvc in pvcs.items:
                    pvc_name = pvc.metadata.name or ""
                    if dep_name in pvc_name:
                        pvc_names.add(pvc_name)
            except ApiException as exc:
                logger.debug("Could not list PVCs in %s: %s", ns, exc.reason)
            for pvc_name in sorted(pvc_names):
                _fetch_by_name(pvc_name)

        return events[: self.cfg.max_events]

    # ------------------------------------------------------------------
    # Fallback: direct kubectl logs (Loki unconfigured, down, or empty)
    # ------------------------------------------------------------------
    def _get_pod_logs(self, ns: str, pod_name: str) -> str:
        try:
            logs = self.core_v1.read_namespaced_pod_log(
                name=pod_name,
                namespace=ns,
                tail_lines=self.cfg.max_log_lines,
                timestamps=True,
            )
            if isinstance(logs, bytes):
                logs = logs.decode("utf-8", errors="replace")
            return logs or "(no logs)"
        except ApiException:
            try:
                logs = self.core_v1.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=ns,
                    previous=True,
                    tail_lines=self.cfg.max_log_lines,
                    timestamps=True,
                )
                if isinstance(logs, bytes):
                    logs = logs.decode("utf-8", errors="replace")
                return f"[Previous container logs]\n{logs}"
            except ApiException as exc:
                return f"(could not fetch logs: {exc.reason})"

    # ------------------------------------------------------------------
    # Deployment spec
    # ------------------------------------------------------------------
    def _get_deployment_spec(self, ns: str, dep_name: str) -> Dict:
        try:
            dep = self.apps_v1.read_namespaced_deployment(name=dep_name, namespace=ns)
            containers = []
            for c in dep.spec.template.spec.containers:
                req = (c.resources.requests or {}) if c.resources else {}
                lim = (c.resources.limits or {}) if c.resources else {}
                containers.append({
                    "name": c.name,
                    "image": c.image,
                    "resources": {
                        "requests": {"cpu": req.get("cpu"), "memory": req.get("memory")},
                        "limits": {"cpu": lim.get("cpu"), "memory": lim.get("memory")},
                    },
                    "liveness_probe": bool(c.liveness_probe),
                    "readiness_probe": bool(c.readiness_probe),
                })
            return {
                "replicas": dep.spec.replicas,
                "strategy": dep.spec.strategy.type if dep.spec.strategy else "RollingUpdate",
                "containers": containers,
            }
        except ApiException as exc:
            logger.warning("Could not fetch deployment spec for %s/%s: %s", ns, dep_name, exc.reason)
            return {}
