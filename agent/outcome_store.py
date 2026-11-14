# Copyright (c) 2026 Ibn Abbas
# SPDX-License-Identifier: MIT
"""
Minimal outcome store — persists remediation outcomes (service, hypothesis,
command, resolved, time-to-resolve) to a ConfigMap so future incidents on the
same service can be grounded with what was tried before and whether it worked.

Minimal viable RAG: exact service+namespace match, most-recent-first, no
embeddings or vector search. Mirrors the alert-cooldown ConfigMap pattern
already used in agent/monitor.py (_load_cooldown_map/_save_cooldown_map).
"""

import json
import logging
from typing import Any, Dict, List

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)

OUTCOMES_CONFIGMAP = "claude-agent-outcomes"
MAX_OUTCOMES = 50  # bounds ConfigMap size — oldest entries are dropped first


def load_outcomes(core_v1: k8s_client.CoreV1Api, namespace: str) -> List[Dict[str, Any]]:
    """Read the full outcome history (oldest first) from the ConfigMap."""
    try:
        cm = core_v1.read_namespaced_config_map(OUTCOMES_CONFIGMAP, namespace)
        raw = (cm.data or {}).get("outcomes", "[]")
        return json.loads(raw)
    except ApiException as exc:
        if exc.status == 404:
            return []
        logger.warning("Could not read outcomes ConfigMap: %s", exc)
        return []
    except json.JSONDecodeError:
        logger.warning("Outcomes ConfigMap contained invalid JSON — ignoring")
        return []


def save_outcome(core_v1: k8s_client.CoreV1Api, namespace: str, outcome: Dict[str, Any]) -> None:
    """Append one outcome record, trimming to the most recent MAX_OUTCOMES."""
    outcomes = load_outcomes(core_v1, namespace)
    outcomes.append(outcome)
    outcomes = outcomes[-MAX_OUTCOMES:]
    data = {"outcomes": json.dumps(outcomes)}
    try:
        try:
            core_v1.read_namespaced_config_map(OUTCOMES_CONFIGMAP, namespace)
            core_v1.patch_namespaced_config_map(OUTCOMES_CONFIGMAP, namespace, {"data": data})
        except ApiException as exc:
            if exc.status == 404:
                core_v1.create_namespaced_config_map(
                    namespace,
                    k8s_client.V1ConfigMap(
                        metadata=k8s_client.V1ObjectMeta(name=OUTCOMES_CONFIGMAP),
                        data=data,
                    ),
                )
            else:
                raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not save outcome to ConfigMap: %s", exc)


def similar_outcomes(
    outcomes: List[Dict[str, Any]], service: str, namespace: str, limit: int = 3
) -> List[Dict[str, Any]]:
    """Exact service+namespace match, most recent first — the retrieval half of the RAG."""
    matches = [o for o in outcomes if o.get("service") == service and o.get("namespace") == namespace]
    return matches[-limit:][::-1]
