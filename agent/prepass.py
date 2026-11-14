# Copyright (c) 2026 Ibn Abbas
# SPDX-License-Identifier: MIT
"""
Cheap deterministic pre-pass — Grafana Sift-style fixed checklist run over data
already collected for this incident (health_report + context). Zero new API
calls; feeds advisory hints into the Claude prompt so well-understood failure
modes don't need a full agentic investigation to be named.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

RECENT_DEPLOY_WINDOW_S = 600  # 10 minutes
PVC_FULL_HINT_PCT = 90.0

# Known past-incident signatures for the *application services* this agent
# monitors — NOT the observability stack's own Prometheus/Grafana pods in the
# `observability` namespace (Rancher-operator-conflict / WAL-death-spiral are
# specific to that StatefulSet and are handled by diagnose-observability.yml,
# a separate workflow — surfacing them here for an unrelated app pod would be
# a misleading hint, not a helpful one).
KNOWN_SIGNATURES: List[Dict[str, str]] = [
    {
        "id": "martin-invalid-flag",
        "pattern": r"--auto-publish",
        "hint": (
            "Matches a known incident: martin tile server exits with code 2 because "
            "--auto-publish is not a valid CLI flag (the correct flag is --auto-bounds). "
            "Check the container args in the deployment spec."
        ),
    },
    {
        "id": "redis-clusterdown",
        "pattern": r"CLUSTERDOWN|cluster_state:\s*fail",
        "hint": (
            "Matches a known incident: Redis cluster_state:fail / CLUSTERDOWN, usually "
            "because a node was force-deleted and rejoined with a new IP. Fix is "
            "typically CLUSTER MEET to re-add the node."
        ),
    },
    # 2026-11-03 Loki incident (docs/open-items.md OI-1)
    {
        "id": "disk-full",
        "pattern": r"no space left on device|ENOSPC|disk quota exceeded",
        "hint": (
            "Matches a known incident: the container's volume is full ('no space left on "
            "device'). Check the PVC usage (kubelet_volume_stats_used_bytes / "
            "kubelet_volume_stats_capacity_bytes) and inodes. Fix = expand the PVC if its "
            "StorageClass allows expansion, then delete the pod to grow the filesystem; and "
            "fix the app's retention (e.g. Loki needs compactor.retention_enabled=true — "
            "limits_config.retention_period alone deletes nothing with TSDB)."
        ),
    },
    {
        "id": "crio-name-reserved",
        "pattern": r"name is reserved",
        "hint": (
            "Matches a known incident: CRI-O 'name is reserved' — a previous CreateContainer "
            "call for this pod is still running inside CRI-O. Either CRI-O is wedged on the "
            "node (other pods on the same node stuck too → restart crio+kubelet) or it is "
            "slowly relabeling a large volume (see the SELinux relabel hint)."
        ),
    },
]
_COMPILED_SIGNATURES = [
    (sig["id"], re.compile(sig["pattern"], re.IGNORECASE), sig["hint"])
    for sig in KNOWN_SIGNATURES
]

_CREATE_TIMEOUT_RE = re.compile(r"context deadline exceeded|name is reserved|RST_STREAM", re.IGNORECASE)


def run_prepass(context: Dict[str, Any]) -> List[str]:
    """Pure, zero-I/O checklist. Never raises — a failed check must not block analysis."""
    hints: List[str] = []
    try:
        health_report = context.get("health_report") or {}
        hints.extend(_check_oom(health_report))
        hints.extend(_check_evicted(health_report))
        hints.extend(_check_recent_rollout(context))
        hints.extend(_check_storage(health_report))
        hints.extend(_check_create_container_timeout(health_report))
        hints.extend(_check_known_signatures(health_report, context))
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Pre-pass check failed, continuing without hints: %s", exc, exc_info=True)
    return hints


def _check_oom(health_report: Dict[str, Any]) -> List[str]:
    hints = []
    for pod in health_report.get("pod_statuses") or []:
        for c in pod.get("containers") or []:
            if c.get("reason") == "OOMKilled" or c.get("last_reason") == "OOMKilled":
                hints.append(
                    f"Container '{c.get('name')}' in pod '{pod.get('name')}' OOMKilled "
                    f"(restart_count={c.get('restart_count')}) — check memory limits vs. "
                    "actual usage before assuming an application bug."
                )
    return hints


def _check_evicted(health_report: Dict[str, Any]) -> List[str]:
    return [
        f"{issue} — a node resource-pressure eviction, not an application bug. Check "
        "node disk/memory pressure before recommending an app fix."
        for issue in (health_report.get("issues") or [])
        if "Evicted" in issue
    ]


def _check_recent_rollout(context: Dict[str, Any]) -> List[str]:
    hints = []
    now = datetime.now(timezone.utc)
    for ev in context.get("events") or []:
        if ev.get("reason") != "ScalingReplicaSet":
            continue
        ts = _parse_event_time(ev.get("last_time") or ev.get("first_time"))
        if ts is None:
            continue
        age_s = (now - ts).total_seconds()
        if 0 <= age_s <= RECENT_DEPLOY_WINDOW_S:
            hints.append(
                f"A rollout ({ev.get('message', 'ScalingReplicaSet')}) happened "
                f"{int(age_s)}s ago — strongly consider a bad deploy/rollback before other "
                "hypotheses. (Deployments only — this signal does not cover StatefulSets.)"
            )
    return hints


def _check_storage(health_report: Dict[str, Any]) -> List[str]:
    """PVC full / resize pending / unbound — from health_checker's storage statuses."""
    hints = []
    for st in health_report.get("storage") or []:
        pvc = st.get("pvc")
        pct = st.get("usage_pct")
        if pct is not None and pct >= PVC_FULL_HINT_PCT:
            path = (
                f"StorageClass '{st.get('storage_class')}' allows expansion — expand the PVC "
                "(kubectl patch pvc ... spec.resources.requests.storage), wait for CAPACITY to "
                "update, then delete the pod to finish the filesystem resize. Do NOT change "
                "the StatefulSet volumeClaimTemplate size in Helm (immutable — breaks helm upgrade)."
                if st.get("expandable") else
                f"StorageClass '{st.get('storage_class')}' does not allow expansion — free "
                "space or migrate the data."
            )
            hints.append(
                f"PVC '{pvc}' is {pct}% full ({st.get('capacity')}). This is very likely the "
                f"root cause of crashes/startup failures for this workload. {path} Then find "
                "why it filled (missing/ineffective retention) so it does not recur."
            )
        if st.get("inode_pct") is not None and st["inode_pct"] >= PVC_FULL_HINT_PCT:
            hints.append(
                f"PVC '{pvc}' inodes {st['inode_pct']}% used — too many small files; "
                "expanding bytes may not help, clean up / enable retention."
            )
        if "FileSystemResizePending" in (st.get("conditions") or []):
            hints.append(
                f"PVC '{pvc}' has FileSystemResizePending — the volume was expanded; delete "
                "the pod once so the kubelet grows the filesystem."
            )
        if st.get("phase") and st["phase"] != "Bound":
            hints.append(
                f"PVC '{pvc}' is {st['phase']} (StorageClass '{st.get('storage_class')}') — "
                "pods mounting it cannot start until it binds; check the provisioner/SC first."
            )
    return hints


def _check_create_container_timeout(health_report: Dict[str, Any]) -> List[str]:
    """
    CreateContainerError with 'context deadline exceeded' / 'name is reserved' on a
    pod that mounts a PVC: CRI-O timed out creating the container. Two known causes
    on stg OKE: (1) CRI-O wedged on the node, (2) recursive SELinux relabel of a
    large volume (happens even with SELinux Permissive).
    """
    hints = []
    has_pvc = bool(health_report.get("storage"))
    node_wedged = any(
        "container runtime (CRI-O) likely wedged" in i for i in (health_report.get("issues") or [])
    )
    for pod in health_report.get("pod_statuses") or []:
        for c in pod.get("containers") or []:
            if c.get("reason") != "CreateContainerError":
                continue
            if not _CREATE_TIMEOUT_RE.search(c.get("message") or ""):
                continue
            if node_wedged:
                hints.append(
                    f"Pod '{pod.get('name')}' CreateContainerError timeout AND other pods on node "
                    f"{pod.get('node')} are stuck — CRI-O is wedged on that node. Fix the node "
                    "(systemctl restart crio kubelet, or cycle it), not the workload."
                )
            elif has_pvc:
                hints.append(
                    f"Pod '{pod.get('name')}' CreateContainerError timeout with a PVC mounted and "
                    "no other stuck pods on the node — most likely CRI-O's recursive SELinux "
                    "relabel of a large volume exceeding the CRI timeout. Fix: pod "
                    "securityContext.seLinuxOptions.type=spc_t and fsGroupChangePolicy="
                    "OnRootMismatch (in the Helm values, not only a live patch). Waiting 10–15 min "
                    "also works once, but every new pod repeats it."
                )
            else:
                hints.append(
                    f"Pod '{pod.get('name')}' CreateContainerError timeout — CRI-O did not finish "
                    "creating the container in time; check CRI-O health on node "
                    f"{pod.get('node')} (journalctl -u crio)."
                )
    return hints


def _parse_event_time(ts_str: Any) -> Optional[datetime]:
    if not ts_str:
        return None
    try:
        ts = datetime.fromisoformat(ts_str)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _check_known_signatures(health_report: Dict[str, Any], context: Dict[str, Any]) -> List[str]:
    parts: List[str] = list(health_report.get("issues") or [])
    parts.append(context.get("loki_logs") or "")
    # kubectl-logs fallback (used when Loki is down or returned nothing)
    parts.extend(str(v) for v in (context.get("pod_logs") or {}).values())
    parts.extend(str(ev.get("message") or "") for ev in (context.get("events") or []))
    for pod in health_report.get("pod_statuses") or []:
        for c in pod.get("containers") or []:
            parts.append(str(c.get("message") or ""))
            parts.append(str(c.get("last_message") or ""))
    haystack = "\n".join(parts)
    return [
        f"Known signature ({sig_id}): {hint}"
        for sig_id, pattern, hint in _COMPILED_SIGNATURES
        if pattern.search(haystack)
    ]
