"""
Storage health checks for a workload's pods — PersistentVolumeClaims and the
StorageClasses behind them.

Added after the 2026-11-03 Loki incident (see docs/open-items.md, OI-1): the
agent saw `CreateContainerError` / `CrashLoopBackOff` on `loki-stack-0` but had
no signal for the real causes — a 100%-full PVC (`no space left on device`)
and a slow CRI-O SELinux relabel of a large volume. These checks surface them
directly so the alert names the storage problem instead of a generic pod error.

Checks per PVC mounted by the workload's pods:
  - PVC missing (404) or not Bound (Pending/Lost), with age
  - StorageClass missing, or no default SC for a PVC without storageClassName
  - Resize state: Resizing / FileSystemResizePending / ControllerResizeError /
    NodeResizeError conditions, and requested > actual capacity
  - Usage via Prometheus kubelet_volume_stats_* (bytes + inodes):
    >= 80% warning, >= 90% critical (writes about to fail)
  - Whether the SC allows expansion (decides the remediation path)

All Prometheus data is fetched ONCE per cycle with cluster-wide queries and
cached — never one query per PVC. Every check degrades gracefully: no
Prometheus, missing metrics, or API errors just skip that check, never raise.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from kubernetes import client
from kubernetes.client.exceptions import ApiException

try:
    from . import observability_clients as obs
except ImportError:
    import observability_clients as obs

try:
    from kubernetes.utils import parse_quantity as _k8s_parse_quantity
except ImportError:  # very old kubernetes client
    _k8s_parse_quantity = None

logger = logging.getLogger(__name__)

PVC_USAGE_WARN = 0.80
PVC_USAGE_CRIT = 0.90
INODE_USAGE_CRIT = 0.90
PVC_PENDING_GRACE_S = 120

RESIZE_CONDITIONS = {
    "Resizing": "volume expansion in progress at the storage backend",
    "FileSystemResizePending": (
        "volume already expanded — filesystem grows when the pod is restarted "
        "(delete the pod to finish the resize)"
    ),
    "ControllerResizeError": "volume expansion FAILED at the storage backend",
    "NodeResizeError": "filesystem expansion FAILED on the node",
}

DEFAULT_SC_ANNOTATIONS = (
    "storageclass.kubernetes.io/is-default-class",
    "storageclass.beta.kubernetes.io/is-default-class",
)

UsageKey = Tuple[str, str]  # (namespace, pvc_name)


def parse_quantity(value: Any) -> Optional[float]:
    """'50Gi' -> bytes as float. Returns None when unparseable."""
    if value is None:
        return None
    if _k8s_parse_quantity is not None:
        try:
            return float(_k8s_parse_quantity(value))
        except (ValueError, TypeError, ArithmeticError):
            return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def human_bytes(num: Optional[float]) -> str:
    if num is None:
        return "?"
    for unit in ("B", "Ki", "Mi", "Gi", "Ti"):
        if abs(num) < 1024 or unit == "Ti":
            return f"{num:.1f}{unit}" if unit != "B" else f"{int(num)}B"
        num /= 1024
    return f"{num:.1f}Ti"


class StorageChecker:
    def __init__(self, core_v1: client.CoreV1Api, prometheus_url: str) -> None:
        self.core_v1 = core_v1
        self.storage_v1 = client.StorageV1Api()
        self.prometheus_url = prometheus_url
        self.reset_cycle_cache()

    def reset_cycle_cache(self) -> None:
        """Call once at the start of each health-check cycle."""
        self._sc_cache: Optional[Dict[str, Any]] = None
        self._default_sc: Optional[str] = None
        self._usage_cache: Optional[Dict[UsageKey, Dict[str, float]]] = None
        self._usage_error: Optional[str] = None

    # ------------------------------------------------------------------
    # Cluster-wide lookups (cached per cycle)
    # ------------------------------------------------------------------
    def _storage_classes(self) -> Dict[str, Any]:
        if self._sc_cache is not None:
            return self._sc_cache
        self._sc_cache = {}
        try:
            for sc in self.storage_v1.list_storage_class().items:
                self._sc_cache[sc.metadata.name] = sc
                ann = sc.metadata.annotations or {}
                if any(ann.get(a) == "true" for a in DEFAULT_SC_ANNOTATIONS):
                    self._default_sc = sc.metadata.name
        except ApiException as exc:
            logger.warning("Could not list StorageClasses: %s", exc.reason)
        return self._sc_cache

    def _volume_usage(self) -> Dict[UsageKey, Dict[str, float]]:
        """{(ns, pvc): {used, capacity, inodes_used, inodes}} from kubelet volume stats."""
        if self._usage_cache is not None:
            return self._usage_cache
        self._usage_cache = {}
        metrics = {
            "used": "kubelet_volume_stats_used_bytes",
            "capacity": "kubelet_volume_stats_capacity_bytes",
            "inodes_used": "kubelet_volume_stats_inodes_used",
            "inodes": "kubelet_volume_stats_inodes",
        }
        for field, metric in metrics.items():
            result, error = obs.prometheus_query(
                self.prometheus_url,
                f"max by (namespace, persistentvolumeclaim) ({metric})",
            )
            if error:
                self._usage_error = error
                logger.debug("PVC usage query failed (%s): %s", metric, error)
                continue
            for series in result:
                labels = series.get("metric", {})
                key = (labels.get("namespace", ""), labels.get("persistentvolumeclaim", ""))
                try:
                    value = float(series.get("value", [0, "nan"])[1])
                except (ValueError, TypeError, IndexError):
                    continue
                self._usage_cache.setdefault(key, {})[field] = value
        if not self._usage_cache and not self._usage_error:
            self._usage_error = "no kubelet_volume_stats_* series in Prometheus"
        return self._usage_cache

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def check_pods_storage(
        self, ns: str, pods: List[client.V1Pod]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Returns (issues, storage_statuses) for every PVC the pods mount."""
        claims: List[str] = []
        for pod in pods:
            for vol in (pod.spec.volumes or []) if pod.spec else []:
                pvc_src = vol.persistent_volume_claim
                if pvc_src and pvc_src.claim_name and pvc_src.claim_name not in claims:
                    claims.append(pvc_src.claim_name)

        issues: List[str] = []
        statuses: List[Dict[str, Any]] = []
        for claim in claims:
            status = self._evaluate_pvc(ns, claim)
            statuses.append(status)
            issues.extend(status["issues"])
        return issues, statuses

    # ------------------------------------------------------------------
    # Per-PVC evaluation
    # ------------------------------------------------------------------
    def _evaluate_pvc(self, ns: str, claim: str) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            "pvc": claim,
            "namespace": ns,
            "phase": None,
            "storage_class": None,
            "expandable": None,
            "requested": None,
            "capacity": None,
            "usage_pct": None,
            "inode_pct": None,
            "conditions": [],
            "issues": [],
        }
        try:
            pvc = self.core_v1.read_namespaced_persistent_volume_claim(claim, ns)
        except ApiException as exc:
            if exc.status == 404:
                status["issues"].append(
                    f"PVC '{claim}' not found in namespace '{ns}' — pods mounting it cannot start"
                )
            else:
                logger.debug("Could not read PVC %s/%s: %s", ns, claim, exc.reason)
            return status

        classes = self._storage_classes()
        phase = pvc.status.phase if pvc.status else None
        status["phase"] = phase

        # StorageClass
        sc_name = pvc.spec.storage_class_name if pvc.spec else None
        if sc_name is None:
            sc_name = self._default_sc
            if sc_name is None and classes and phase != "Bound":
                status["issues"].append(
                    f"PVC '{claim}' has no storageClassName and the cluster has no default "
                    "StorageClass — it can never be provisioned"
                )
        status["storage_class"] = sc_name
        sc = classes.get(sc_name) if sc_name else None
        if sc_name and classes and sc is None:
            status["issues"].append(
                f"PVC '{claim}' uses StorageClass '{sc_name}' which does not exist "
                f"(available: {sorted(classes)})"
            )
        if sc is not None:
            status["expandable"] = bool(sc.allow_volume_expansion)

        # Phase
        if phase and phase != "Bound":
            age = _age_seconds(pvc.metadata.creation_timestamp)
            if phase == "Lost":
                status["issues"].append(
                    f"PVC '{claim}' is Lost — its PersistentVolume is gone; data may be unrecoverable"
                )
            elif age > PVC_PENDING_GRACE_S:
                wffc = sc is not None and sc.volume_binding_mode == "WaitForFirstConsumer"
                hint = (
                    " (WaitForFirstConsumer — binds only after the pod is scheduled; "
                    "check pod scheduling)" if wffc
                    else " (check the CSI provisioner / storage quota / StorageClass parameters)"
                )
                status["issues"].append(f"PVC '{claim}' is {phase} for {int(age)}s{hint}")

        # Requested vs actual capacity
        requested = parse_quantity(
            ((pvc.spec.resources.requests or {}) if pvc.spec and pvc.spec.resources else {}).get("storage")
        )
        capacity = parse_quantity(((pvc.status.capacity or {}) if pvc.status else {}).get("storage"))
        status["requested"] = human_bytes(requested)
        status["capacity"] = human_bytes(capacity)

        # Resize conditions
        resize_reported = False
        for cond in (pvc.status.conditions or []) if pvc.status else []:
            if cond.type in RESIZE_CONDITIONS and cond.status == "True":
                status["conditions"].append(cond.type)
                detail = f": {cond.message}" if cond.message else ""
                status["issues"].append(f"PVC '{claim}' {cond.type} — {RESIZE_CONDITIONS[cond.type]}{detail}")
                resize_reported = True
        if requested and capacity and requested > capacity and not resize_reported and phase == "Bound":
            status["issues"].append(
                f"PVC '{claim}' requests {human_bytes(requested)} but capacity is "
                f"{human_bytes(capacity)} — expansion requested but not applied"
                + ("" if status["expandable"] is not False else
                   f" (StorageClass '{sc_name}' has allowVolumeExpansion=false)")
            )

        # Usage (bytes + inodes) from Prometheus
        usage = self._volume_usage().get((ns, claim))
        if usage:
            used, cap = usage.get("used"), usage.get("capacity")
            if used is not None and cap:
                pct = used / cap
                status["usage_pct"] = round(pct * 100, 1)
                if pct >= PVC_USAGE_WARN:
                    level = "CRITICAL" if pct >= PVC_USAGE_CRIT else "WARNING"
                    effect = (
                        "writes are failing or about to fail ('no space left on device')"
                        if pct >= PVC_USAGE_CRIT else "will fill soon"
                    )
                    status["issues"].append(
                        f"{level}: PVC '{claim}' is {status['usage_pct']}% full "
                        f"({human_bytes(used)}/{human_bytes(cap)}) — {effect}. "
                        + self._expansion_hint(ns, claim, cap, sc_name, status["expandable"])
                    )
            i_used, i_total = usage.get("inodes_used"), usage.get("inodes")
            if i_used is not None and i_total:
                ipct = i_used / i_total
                status["inode_pct"] = round(ipct * 100, 1)
                if ipct >= INODE_USAGE_CRIT:
                    status["issues"].append(
                        f"CRITICAL: PVC '{claim}' inodes {status['inode_pct']}% used — "
                        "'no space left on device' even with free bytes; too many small files "
                        "(clean up or enable retention — expanding the volume may not help)"
                    )
        return status

    @staticmethod
    def _expansion_hint(
        ns: str, claim: str, cap: float, sc_name: Optional[str], expandable: Optional[bool]
    ) -> str:
        if expandable:
            new_gi = max(int(cap * 2 / (1024 ** 3)), 50)  # OCI BV minimum is 50Gi
            return (
                f"StorageClass '{sc_name}' allows expansion: "
                f"kubectl -n {ns} patch pvc {claim} -p "
                f"'{{\"spec\":{{\"resources\":{{\"requests\":{{\"storage\":\"{new_gi}Gi\"}}}}}}}}' "
                "then delete the pod to finish the filesystem resize. Also fix the app's "
                "retention so it does not refill."
            )
        if expandable is False:
            return (
                f"StorageClass '{sc_name}' does NOT allow expansion — free space "
                "(retention/cleanup) or migrate to an expandable class."
            )
        return "Check the StorageClass for allowVolumeExpansion before resizing."


def _age_seconds(ts: Any) -> float:
    from datetime import datetime, timezone
    if ts is None:
        return 0.0
    if getattr(ts, "tzinfo", None) is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max((datetime.now(timezone.utc) - ts).total_seconds(), 0.0)
