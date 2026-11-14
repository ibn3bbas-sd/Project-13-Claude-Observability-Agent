# Copyright 2026 Ibn Abbas
# SPDX-License-Identifier: Apache-2.0
"""
Discovers and evaluates the health of Kubernetes Deployments / Services / Ingresses
in the configured stage namespace(s).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from kubernetes import client, config as k8s_config
from kubernetes.client.exceptions import ApiException

try:
    from .config import Config
    from .storage_checker import StorageChecker
except ImportError:
    from config import Config
    from storage_checker import StorageChecker

logger = logging.getLogger(__name__)

# Pod phases that are always unhealthy
UNHEALTHY_PHASES = {"Failed", "Unknown"}

# Container waiting/terminated reasons that signal a problem
UNHEALTHY_REASONS = {
    # Image pull failures
    "ImagePullBackOff",
    "ErrImagePull",
    "ErrImageNeverPull",
    "ImageInspectError",
    "RegistryUnavailable",
    "InvalidImageName",
    # Container lifecycle failures
    "CrashLoopBackOff",
    "OOMKilled",
    "Error",
    "RunContainerError",
    "CreateContainerConfigError",
    "CreateContainerError",
    "PostStartHookError",
}

# Restart count thresholds
RESTART_WARN = 5     # unstable — needs investigation
RESTART_CRIT = 20    # death spiral — needs immediate action

# Age thresholds for time-based stuck-state detection (seconds)
STUCK_CONTAINER_CREATING_S = 120   # ContainerCreating > 2 min → volume/secret mount issue
STUCK_PENDING_S = 120              # Pending > 2 min → scheduling issue
STUCK_TERMINATING_S = 120          # Terminating > 2 min → finalizer/volume unmount blocked

# Node container-runtime wedge: this many pods on ONE node (any namespace) stuck
# creating containers → the runtime (CRI-O) is the problem, not the workload.
# Seen 2026-09-25 and 2026-11-03 on stg OKE (see docs/open-items.md OI-1).
RUNTIME_WEDGE_MIN_PODS = 2


def _load_k8s(cfg: Config) -> None:
    if cfg.use_in_cluster:
        k8s_config.load_incluster_config()
        logger.info("Using in-cluster Kubernetes config")
    elif cfg.kubeconfig_path:
        k8s_config.load_kube_config(config_file=cfg.kubeconfig_path)
        logger.info("Using kubeconfig: %s", cfg.kubeconfig_path)
    else:
        k8s_config.load_kube_config()
        logger.info("Using default kubeconfig (~/.kube/config)")


def _age_seconds(ts: Any) -> float:
    """Return seconds elapsed since a K8s timestamp (handles tz-aware and tz-naive)."""
    if ts is None:
        return 0.0
    now = datetime.now(timezone.utc)
    if hasattr(ts, "tzinfo") and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max((now - ts).total_seconds(), 0.0)


def _pod_stuck_creating(pod: client.V1Pod) -> Optional[str]:
    """Return the stuck reason if a pod is stuck creating containers, else None."""
    if pod.status is None or pod.status.phase in ("Succeeded", "Failed"):
        return None
    for cs in (pod.status.container_statuses or []):
        if not (cs.state and cs.state.waiting):
            continue
        reason = cs.state.waiting.reason or ""
        if reason == "CreateContainerError":
            return reason
        if reason == "ContainerCreating" and \
                _age_seconds(pod.metadata.creation_timestamp) > STUCK_CONTAINER_CREATING_S:
            return reason
    if pod.status.phase == "Pending" and not pod.status.container_statuses and pod.spec and \
            pod.spec.node_name and _age_seconds(pod.metadata.creation_timestamp) > STUCK_CONTAINER_CREATING_S:
        return "ContainerCreating"
    return None


class HealthChecker:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        _load_k8s(cfg)
        self.apps_v1 = client.AppsV1Api()
        self.core_v1 = client.CoreV1Api()
        self.networking_v1 = client.NetworkingV1Api()
        # Cached IngressClass names — populated once per health-check cycle
        self._ingress_class_names: Optional[Set[str]] = None
        # PVC / StorageClass checks (Prometheus kubelet_volume_stats + API)
        self.storage = StorageChecker(self.core_v1, cfg.prometheus_url)
        # node name → list of "ns/pod (reason)" stuck creating — cached per cycle
        self._node_runtime_cache: Dict[str, List[str]] = {}

    # ------------------------------------------------------------------
    # 1. Discover services (Deployments + StatefulSets)
    # ------------------------------------------------------------------
    def discover_stage_services(self) -> List[Dict[str, Any]]:
        # New cycle — drop per-cycle caches
        self.storage.reset_cycle_cache()
        self._node_runtime_cache = {}

        services = []
        for ns in self.cfg.namespaces:
            try:
                kwargs: Dict[str, Any] = {"namespace": ns}
                if self.cfg.stage_label_selector:
                    kwargs["label_selector"] = self.cfg.stage_label_selector
                deployments = self.apps_v1.list_namespaced_deployment(**kwargs)
                for dep in deployments.items:
                    services.append({
                        "name": dep.metadata.name,
                        "namespace": dep.metadata.namespace,
                        "labels": dep.metadata.labels or {},
                        "selector": dep.spec.selector.match_labels or {},
                        "_deployment": dep,
                        "_statefulset": None,
                    })
            except ApiException as exc:
                logger.error("Failed to list deployments in namespace %s: %s", ns, exc.reason)
            try:
                statefulsets = self.apps_v1.list_namespaced_stateful_set(namespace=ns)
                for sts in statefulsets.items:
                    services.append({
                        "name": sts.metadata.name,
                        "namespace": sts.metadata.namespace,
                        "labels": sts.metadata.labels or {},
                        "selector": (sts.spec.selector.match_labels or {}) if sts.spec.selector else {},
                        "_deployment": None,
                        "_statefulset": sts,
                    })
            except ApiException as exc:
                logger.error("Failed to list statefulsets in namespace %s: %s", ns, exc.reason)
        return services

    # ------------------------------------------------------------------
    # 2. Check health of one service (Deployment or StatefulSet + K8s Service + Pods)
    # ------------------------------------------------------------------
    def check_service_health(self, svc: Dict[str, Any]) -> Dict[str, Any]:
        dep = svc.get("_deployment")
        sts = svc.get("_statefulset")
        workload = dep or sts
        ns = svc["namespace"]
        name = svc["name"]

        report: Dict[str, Any] = {
            "name": name,
            "namespace": ns,
            "healthy": True,
            "summary": "All replicas healthy",
            "issues": [],
            "desired_replicas": 0,
            "ready_replicas": 0,
            "available_replicas": 0,
            "pod_statuses": [],
            "storage": [],
        }

        desired = (workload.spec.replicas or 0) if workload else 0
        status = workload.status if workload else None
        ready = (status.ready_replicas or 0) if status else 0
        # Deployments have available_replicas; StatefulSets use current_replicas
        available = (
            getattr(status, "available_replicas", None)
            or getattr(status, "current_replicas", None)
            or 0
        ) if status else 0

        report["desired_replicas"] = desired
        report["ready_replicas"] = ready
        report["available_replicas"] = available

        if ready < desired:
            report["healthy"] = False
            report["issues"].append(f"Only {ready}/{desired} replicas are ready")

        # Pod-level checks
        label_selector = ",".join(f"{k}={v}" for k, v in (svc["selector"] or {}).items())
        pod_statuses = []
        pod_items: List[client.V1Pod] = []
        try:
            pods = self.core_v1.list_namespaced_pod(namespace=ns, label_selector=label_selector)
            pod_items = list(pods.items)
            for pod in pod_items:
                pod_info = self._evaluate_pod(pod)
                pod_statuses.append(pod_info)
                if not pod_info["healthy"]:
                    report["healthy"] = False
                    report["issues"].extend(pod_info["issues"])
        except ApiException as exc:
            logger.error("Failed to list pods for %s/%s: %s", ns, name, exc.reason)
            report["issues"].append(f"Could not list pods: {exc.reason}")
            report["healthy"] = False

        report["pod_statuses"] = pod_statuses

        # Storage checks — PVCs mounted by the pods + their StorageClasses
        try:
            storage_issues, storage_statuses = self.storage.check_pods_storage(ns, pod_items)
            report["storage"] = storage_statuses
            if storage_issues:
                report["healthy"] = False
                report["issues"].extend(storage_issues)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Storage check failed for %s/%s: %s", ns, name, exc, exc_info=True)

        # Node container-runtime wedge (many pods on one node stuck creating)
        try:
            runtime_issues = self._check_node_runtime(pod_items)
            if runtime_issues:
                report["healthy"] = False
                report["issues"].extend(runtime_issues)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Node runtime check failed for %s/%s: %s", ns, name, exc, exc_info=True)

        # K8s Service-level checks (endpoint, selector, LB, targetPort)
        try:
            svc_obj = self.core_v1.read_namespaced_service(name, ns)
            self._check_k8s_service(svc_obj, ns, name, desired, report)
        except ApiException:
            pass  # No K8s Service with this name — skip

        if report["issues"]:
            report["summary"] = "; ".join(report["issues"][:3])

        return report

    def _check_node_runtime(self, pods: List[client.V1Pod]) -> List[str]:
        """
        If this workload's pod is stuck creating containers, look at every pod on
        the same node. Several stuck pods from different workloads = the node's
        container runtime is wedged (CRI-O 'name is reserved' / 'context deadline
        exceeded'), which no workload-level fix will solve.
        """
        issues: List[str] = []
        seen_nodes: Set[str] = set()
        for pod in pods:
            if not _pod_stuck_creating(pod):
                continue
            node = pod.spec.node_name if pod.spec else None
            if not node or node in seen_nodes:
                continue
            seen_nodes.add(node)
            if node not in self._node_runtime_cache:
                stuck: List[str] = []
                try:
                    node_pods = self.core_v1.list_pod_for_all_namespaces(
                        field_selector=f"spec.nodeName={node}"
                    )
                    for npod in node_pods.items:
                        reason = _pod_stuck_creating(npod)
                        if reason:
                            stuck.append(f"{npod.metadata.namespace}/{npod.metadata.name} ({reason})")
                except ApiException as exc:
                    logger.debug("Could not list pods on node %s: %s", node, exc.reason)
                self._node_runtime_cache[node] = stuck
            stuck = self._node_runtime_cache[node]
            if len(stuck) >= RUNTIME_WEDGE_MIN_PODS:
                issues.append(
                    f"Node {node}: {len(stuck)} pods stuck creating containers across workloads "
                    f"({', '.join(stuck[:5])}) — container runtime (CRI-O) likely wedged. "
                    "Restart crio+kubelet on the node (or cycle the node); deleting the pod alone "
                    "will not help."
                )
        return issues

    def _check_k8s_service(
        self,
        svc_obj: client.V1Service,
        ns: str,
        name: str,
        desired: int,
        report: Dict[str, Any],
    ) -> None:
        spec = svc_obj.spec or client.V1ServiceSpec()
        selector = spec.selector or {}

        # Empty selector — endpoints never auto-populated
        if not selector and spec.type not in ("ExternalName",):
            report["issues"].append(
                f"K8s Service '{name}' has no selector — endpoints must be managed manually"
            )

        # LoadBalancer pending external IP
        if spec.type == "LoadBalancer":
            lb_ips = []
            if (svc_obj.status
                    and svc_obj.status.load_balancer
                    and svc_obj.status.load_balancer.ingress):
                lb_ips = [
                    i.ip or i.hostname
                    for i in svc_obj.status.load_balancer.ingress
                    if i.ip or i.hostname
                ]
            if not lb_ips:
                age = _age_seconds(svc_obj.metadata.creation_timestamp)
                if age > 120:
                    report["healthy"] = False
                    report["issues"].append(
                        f"Service '{name}' (LoadBalancer) has no external IP after {int(age)}s"
                    )

        # Endpoint ready/not-ready addresses
        try:
            ep = self.core_v1.read_namespaced_endpoints(name, ns)
            ready_addrs = sum(
                len(subset.addresses or []) for subset in (ep.subsets or [])
            )
            not_ready_addrs = sum(
                len(subset.not_ready_addresses or []) for subset in (ep.subsets or [])
            )
            if ready_addrs == 0 and desired > 0:
                report["healthy"] = False
                if not_ready_addrs > 0:
                    report["issues"].append(
                        f"K8s Service has 0 ready endpoints ({not_ready_addrs} not-ready) — "
                        "readiness probe may be failing"
                    )
                elif selector:
                    report["issues"].append(
                        f"K8s Service has 0 endpoints — selector {selector} matches no running pods"
                    )
                else:
                    report["issues"].append(
                        "K8s Service has 0 ready endpoints (no pods matched)"
                    )
        except ApiException:
            pass

        # targetPort cross-reference with matched pod containerPorts
        if selector:
            self._check_target_port_mismatch(svc_obj, ns, name, report)

    def _check_target_port_mismatch(
        self,
        svc_obj: client.V1Service,
        ns: str,
        svc_name: str,
        report: Dict[str, Any],
    ) -> None:
        spec = svc_obj.spec or client.V1ServiceSpec()
        selector_str = ",".join(f"{k}={v}" for k, v in (spec.selector or {}).items())
        if not selector_str:
            return
        try:
            pods = self.core_v1.list_namespaced_pod(ns, label_selector=selector_str)
            if not pods.items:
                return
            # Collect containerPort numbers and named ports from matched pods (sample 3)
            known_port_numbers: Set[int] = set()
            known_port_names: Set[str] = set()
            for pod in pods.items[:3]:
                for c in (pod.spec.containers or []):
                    for p in (c.ports or []):
                        if p.container_port:
                            known_port_numbers.add(p.container_port)
                        if p.name:
                            known_port_names.add(p.name)
            if not known_port_numbers and not known_port_names:
                return  # pods declare no containerPorts — skip

            for svc_port in (spec.ports or []):
                tp = svc_port.target_port
                if tp is None:
                    continue
                if isinstance(tp, int):
                    if known_port_numbers and tp not in known_port_numbers:
                        report["issues"].append(
                            f"Service '{svc_name}' port {svc_port.port}: "
                            f"targetPort {tp} not found in pod containerPorts "
                            f"{sorted(known_port_numbers)}"
                        )
                        report["healthy"] = False
                else:
                    tp_str = str(tp)
                    if known_port_names and tp_str not in known_port_names:
                        report["issues"].append(
                            f"Service '{svc_name}' port {svc_port.port}: "
                            f"named targetPort '{tp_str}' not found in pod named ports "
                            f"{sorted(known_port_names)}"
                        )
                        report["healthy"] = False
        except ApiException:
            pass

    # ------------------------------------------------------------------
    # 3. Pod evaluation
    # ------------------------------------------------------------------
    def _evaluate_pod(self, pod: client.V1Pod) -> Dict[str, Any]:
        pod_name = pod.metadata.name
        phase = pod.status.phase or "Unknown"
        issues: List[str] = []

        # Evicted pod (phase=Failed, status.reason=Evicted)
        if phase == "Failed" and getattr(pod.status, "reason", None) == "Evicted":
            issues.append(
                f"Pod {pod_name} was Evicted — node ran out of disk/memory/PID resources"
            )
        elif phase in UNHEALTHY_PHASES:
            issues.append(f"Pod {pod_name} in phase {phase}")

        # Pending stuck > threshold — check PodScheduled condition
        if phase == "Pending":
            age = _age_seconds(pod.metadata.creation_timestamp)
            if age > STUCK_PENDING_S:
                schedule_reason = ""
                for cond in (pod.status.conditions or []):
                    if cond.type == "PodScheduled" and cond.status == "False":
                        schedule_reason = cond.reason or cond.message or "no nodes available"
                        break
                if schedule_reason:
                    issues.append(
                        f"Pod {pod_name} Unschedulable: {schedule_reason} "
                        f"(pending {int(age)}s — check resources/taints/affinity)"
                    )
                else:
                    issues.append(
                        f"Pod {pod_name} stuck Pending for {int(age)}s"
                    )

        # Terminating stuck > threshold
        if pod.metadata.deletion_timestamp:
            term_age = _age_seconds(pod.metadata.deletion_timestamp)
            if term_age > STUCK_TERMINATING_S:
                issues.append(
                    f"Pod {pod_name} stuck Terminating for {int(term_age)}s "
                    "(finalizer or volume unmount blocked — may need force-delete)"
                )

        # Pod conditions (Running but containers not ready / init not complete)
        if phase == "Running":
            for cond in (pod.status.conditions or []):
                if cond.type == "ContainersReady" and cond.status == "False":
                    msg = cond.message or "readiness probe failing"
                    issues.append(f"Pod {pod_name} ContainersReady=False: {msg}")
                    break
                if cond.type == "Initialized" and cond.status == "False":
                    issues.append(f"Pod {pod_name} Initialized=False — init containers not complete")

        # Container-level checks
        containers = []
        for cs in (pod.status.container_statuses or []):
            c_info: Dict[str, Any] = {
                "name": cs.name,
                "ready": cs.ready,
                "restart_count": cs.restart_count,
                "reason": None,
                "message": None,
                "last_reason": None,
                "last_message": None,
            }

            # Waiting state
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                c_info["reason"] = reason
                c_info["message"] = cs.state.waiting.message
                if reason in UNHEALTHY_REASONS:
                    detail = f" ({cs.state.waiting.message[:160]})" if cs.state.waiting.message else ""
                    issues.append(f"Container {cs.name}: {reason}{detail}")
                elif reason == "ContainerCreating":
                    # Only an issue if stuck beyond threshold
                    age = _age_seconds(pod.metadata.creation_timestamp)
                    if age > STUCK_CONTAINER_CREATING_S:
                        issues.append(
                            f"Container {cs.name} stuck ContainerCreating for {int(age)}s "
                            "(likely a PVC / Secret / ConfigMap mount failure)"
                        )

            # Terminated state
            elif cs.state and cs.state.terminated:
                reason = cs.state.terminated.reason or ""
                c_info["reason"] = reason
                if reason in UNHEALTHY_REASONS:
                    issues.append(
                        f"Container {cs.name} terminated: {reason} "
                        f"(exit {cs.state.terminated.exit_code})"
                    )

            # Last termination reason — visible even after the container has since
            # restarted into Waiting/CrashLoopBackOff, so an OOMKilled cause isn't
            # missed just because the container has already restarted by the time
            # we check (the common case for a crash-looping pod).
            if cs.last_state and cs.last_state.terminated:
                last_reason = cs.last_state.terminated.reason or ""
                c_info["last_reason"] = last_reason
                c_info["last_message"] = cs.last_state.terminated.message
                if last_reason == "OOMKilled" and c_info["reason"] != "OOMKilled":
                    issues.append(
                        f"Container {cs.name} was previously OOMKilled "
                        f"(restart_count={cs.restart_count})"
                    )

            # Two-tier restart threshold
            if cs.restart_count and cs.restart_count >= RESTART_CRIT:
                issues.append(
                    f"Container {cs.name} has {cs.restart_count} restarts — "
                    "CRITICAL: possible death spiral, consider deleting PVC/pod"
                )
            elif cs.restart_count and cs.restart_count >= RESTART_WARN:
                issues.append(f"Container {cs.name} has {cs.restart_count} restarts")

            containers.append(c_info)

        # Init container checks
        for ics in (pod.status.init_container_statuses or []):
            if ics.state and ics.state.waiting:
                reason = ics.state.waiting.reason or ""
                if reason in UNHEALTHY_REASONS:
                    issues.append(f"Init container {ics.name}: {reason}")
                elif reason == "PodInitializing":
                    age = _age_seconds(pod.metadata.creation_timestamp)
                    if age > 300:
                        issues.append(
                            f"Init container {ics.name} stuck PodInitializing for {int(age)}s"
                        )

        return {
            "name": pod_name,
            "phase": phase,
            "node": pod.spec.node_name if pod.spec else None,
            "containers": containers,
            "healthy": len(issues) == 0,
            "issues": issues,
        }

    # ------------------------------------------------------------------
    # 4. Discover ingresses (builds cluster-wide hostname duplicate map)
    # ------------------------------------------------------------------
    def discover_stage_ingresses(self) -> List[Dict[str, Any]]:
        ingresses = []
        # hostname → list of {"owner": "ns/name", "paths": frozenset}
        hostname_map: Dict[str, List[Dict[str, Any]]] = {}

        for ns in self.cfg.namespaces:
            try:
                ing_list = self.networking_v1.list_namespaced_ingress(namespace=ns)
                for ing in ing_list.items:
                    for rule in (ing.spec.rules or []):
                        if rule.host:
                            paths: frozenset = frozenset(
                                p.path
                                for p in (rule.http.paths if rule.http else [])
                                if p.path
                            )
                            hostname_map.setdefault(rule.host, []).append(
                                {"owner": f"{ns}/{ing.metadata.name}", "paths": paths}
                            )
                    ingresses.append({
                        "name": ing.metadata.name,
                        "namespace": ing.metadata.namespace,
                        "labels": ing.metadata.labels or {},
                        "selector": {},
                        "_ingress": ing,
                        "_type": "ingress",
                    })
            except ApiException as exc:
                logger.error("Failed to list ingresses in %s: %s", ns, exc.reason)

        # Attach duplicate-host info only when paths genuinely overlap (or are absent)
        for ing_svc in ingresses:
            ing = ing_svc["_ingress"]
            duplicates: Dict[str, List[str]] = {}
            for rule in (ing.spec.rules or []):
                if rule.host:
                    entries = hostname_map.get(rule.host, [])
                    if len(entries) > 1 and self._paths_conflict(entries):
                        duplicates[rule.host] = [e["owner"] for e in entries]
            ing_svc["_duplicate_hosts"] = duplicates

        return ingresses

    @staticmethod
    def _paths_conflict(entries: List[Dict[str, Any]]) -> bool:
        """True if any two ingress entries for the same hostname have overlapping or absent paths."""
        for i, a in enumerate(entries):
            for b in entries[i + 1:]:
                # Either has no paths → catch-all, will shadow the other
                if not a["paths"] or not b["paths"]:
                    return True
                # Shared path(s) → non-deterministic routing
                if a["paths"] & b["paths"]:
                    return True
        return False

    def _get_ingress_class_names(self) -> Set[str]:
        """Return available IngressClass names (cached per cycle)."""
        if self._ingress_class_names is not None:
            return self._ingress_class_names
        try:
            classes = self.networking_v1.list_ingress_class()
            self._ingress_class_names = {ic.metadata.name for ic in classes.items}
        except ApiException:
            self._ingress_class_names = set()
        return self._ingress_class_names

    # ------------------------------------------------------------------
    # 5. Check health of one ingress
    # ------------------------------------------------------------------
    def check_ingress_health(self, svc: Dict[str, Any]) -> Dict[str, Any]:
        ing = svc["_ingress"]
        ns = svc["namespace"]
        name = svc["name"]
        issues: List[str] = []
        backend_statuses: List[Dict] = []
        total_backends = 0
        ready_backends = 0

        try:
            existing_svcs = [
                s.metadata.name
                for s in self.core_v1.list_namespaced_service(ns).items
            ]
        except ApiException:
            existing_svcs = []
        svc["_existing_services"] = existing_svcs

        # IngressClass checks
        class_name = ing.spec.ingress_class_name
        annotation_class = (ing.metadata.annotations or {}).get("kubernetes.io/ingress.class")
        if not class_name and not annotation_class:
            issues.append(
                "No ingressClassName set — ingress may not be picked up by any controller"
            )
        elif class_name:
            known_classes = self._get_ingress_class_names()
            if known_classes and class_name not in known_classes:
                issues.append(
                    f"IngressClass '{class_name}' not found in cluster "
                    f"(available: {sorted(known_classes)})"
                )

        # LB address — with age-based messaging
        lb_addrs = []
        if ing.status and ing.status.load_balancer and ing.status.load_balancer.ingress:
            lb_addrs = [
                i.ip or i.hostname
                for i in ing.status.load_balancer.ingress
                if i.ip or i.hostname
            ]
        if not lb_addrs:
            age = _age_seconds(ing.metadata.creation_timestamp)
            if age > 180:
                issues.append(
                    f"No LB address assigned after {int(age)}s — "
                    "ingress controller may not be running or LB provisioning failed"
                )
            else:
                issues.append("No LB address assigned to ingress (may still be provisioning)")

        # TLS secret checks
        for tls_entry in (ing.spec.tls or []):
            secret_name = tls_entry.secret_name
            if not secret_name:
                continue
            try:
                secret = self.core_v1.read_namespaced_secret(secret_name, ns)
                if secret.type != "kubernetes.io/tls":
                    issues.append(
                        f"TLS secret '{secret_name}' has wrong type '{secret.type}' "
                        f"(expected kubernetes.io/tls) — HTTPS will not work"
                    )
            except ApiException as exc:
                if exc.status == 404:
                    issues.append(
                        f"TLS secret '{secret_name}' not found in namespace '{ns}' — "
                        "HTTPS will fail (cert-manager may not have issued yet)"
                    )

        # Duplicate hostname check
        for host, owners in (svc.get("_duplicate_hosts") or {}).items():
            issues.append(
                f"Hostname '{host}' claimed by {len(owners)} ingresses {owners} — "
                "routing will be non-deterministic"
            )

        # No rules and no default backend
        if not ing.spec.rules and not ing.spec.default_backend:
            issues.append(
                "Ingress has no rules and no defaultBackend — no traffic will be routed"
            )

        # Per-rule backend checks
        for rule in (ing.spec.rules or []):
            host = rule.host or "*"
            for path_obj in (rule.http.paths if rule.http else []):
                backend = path_obj.backend
                svc_name = backend.service.name if backend.service else None
                svc_port_num = (
                    backend.service.port.number
                    if (backend.service and backend.service.port)
                    else None
                )
                svc_port_name_ref = (
                    backend.service.port.name
                    if (backend.service and backend.service.port)
                    else None
                )
                total_backends += 1

                if not svc_name:
                    issues.append(f"{host}: no backend service configured")
                    backend_statuses.append({
                        "name": f"{host}{path_obj.path}",
                        "healthy": False,
                        "issues": ["no backend service name"],
                    })
                    continue

                try:
                    backend_svc_obj = self.core_v1.read_namespaced_service(svc_name, ns)
                    svc_type = backend_svc_obj.spec.type

                    # Backend port mismatch
                    if svc_port_num is not None:
                        available_ports = {
                            p.port for p in (backend_svc_obj.spec.ports or [])
                        }
                        if available_ports and svc_port_num not in available_ports:
                            issues.append(
                                f"Backend '{svc_name}': port {svc_port_num} not in Service spec "
                                f"(available: {sorted(available_ports)})"
                            )
                    elif svc_port_name_ref is not None:
                        available_port_names = {
                            p.name for p in (backend_svc_obj.spec.ports or []) if p.name
                        }
                        if available_port_names and svc_port_name_ref not in available_port_names:
                            issues.append(
                                f"Backend '{svc_name}': named port '{svc_port_name_ref}' not in "
                                f"Service spec (available: {sorted(available_port_names)})"
                            )

                    if svc_type == "ExternalName":
                        ready_backends += 1
                        backend_statuses.append({
                            "name": f"{svc_name}:{svc_port_num or svc_port_name_ref}",
                            "healthy": True,
                            "issues": [],
                        })
                    else:
                        ep = self.core_v1.read_namespaced_endpoints(svc_name, ns)
                        ready = sum(
                            len(subset.addresses or []) for subset in (ep.subsets or [])
                        )
                        if ready > 0:
                            ready_backends += 1
                            backend_statuses.append({
                                "name": f"{svc_name}:{svc_port_num or svc_port_name_ref}",
                                "healthy": True,
                                "ready_endpoints": ready,
                                "issues": [],
                            })
                        else:
                            issues.append(f"Backend {svc_name}: 0 ready endpoints")
                            backend_statuses.append({
                                "name": f"{svc_name}:{svc_port_num or svc_port_name_ref}",
                                "healthy": False,
                                "ready_endpoints": 0,
                                "issues": [
                                    "0 ready endpoints — pods may not be passing readiness probe"
                                ],
                            })

                except ApiException as exc:
                    if exc.status == 404:
                        similar = [
                            s for s in existing_svcs if svc_name in s or s in svc_name
                        ]
                        hint = (
                            f" — similar services exist: {similar}"
                            if similar
                            else f" — services in namespace: {existing_svcs[:8]}"
                        )
                        issues.append(
                            f"Backend {svc_name}: service not found in namespace{hint}"
                        )
                    else:
                        issues.append(f"Backend {svc_name}: lookup failed ({exc.reason})")
                    backend_statuses.append({
                        "name": svc_name,
                        "healthy": False,
                        "issues": [f"service not found: {exc.reason}"],
                    })

        return {
            "name": name,
            "namespace": ns,
            "healthy": len(issues) == 0,
            "summary": "; ".join(issues[:3]) if issues else "All ingress backends healthy",
            "issues": issues,
            "desired_replicas": total_backends,
            "ready_replicas": ready_backends,
            "available_replicas": ready_backends,
            "pod_statuses": backend_statuses,
        }
