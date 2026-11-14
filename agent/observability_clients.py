# Copyright (c) 2026 Ibn Abbas
# SPDX-License-Identifier: MIT
"""
Thin HTTP clients for Prometheus, Loki, and Tempo — shared between
ClaudeAnalyzer's on-demand investigation tools (agent/claude_analyzer.py) and
ContextCollector's identity-aware correlated timeline pre-fetch
(agent/context_collector.py).

Every query function returns (data, error): `error` is None on success (even
if `data` is empty — that's a real "no results" answer, not a failure), or a
short human-readable string describing what went wrong (unconfigured URL,
non-200 response, network exception). Callers that surface errors to Claude
(the tool-use case) show `error` directly; callers doing a best-effort
background pre-fetch (the correlated-timeline case) can just treat any error
as "no data available" and move on.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

HTTP_TIMEOUT = 10

QueryResult = Tuple[List[Dict[str, Any]], Optional[str]]


def prometheus_query(url: str, promql: str) -> QueryResult:
    """Instant PromQL query."""
    if not url:
        return [], "Prometheus is not configured (PROMETHEUS_URL unset)."
    try:
        resp = requests.get(f"{url}/api/v1/query", params={"query": promql}, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return [], f"Prometheus returned HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json().get("data", {}).get("result", []), None
    except Exception as exc:  # pylint: disable=broad-except
        return [], f"Prometheus query failed: {exc}"


def prometheus_query_range(url: str, promql: str, range_minutes: int) -> QueryResult:
    """Range PromQL query over the last `range_minutes`."""
    if not url:
        return [], "Prometheus is not configured (PROMETHEUS_URL unset)."
    try:
        now = time.time()
        resp = requests.get(
            f"{url}/api/v1/query_range",
            params={"query": promql, "start": now - range_minutes * 60, "end": now, "step": "30s"},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return [], f"Prometheus returned HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json().get("data", {}).get("result", []), None
    except Exception as exc:  # pylint: disable=broad-except
        return [], f"Prometheus query failed: {exc}"


def loki_query_range(url: str, logql: str, range_minutes: int, limit: int = 50) -> QueryResult:
    """Returns raw Loki 'result' streams: [{"stream": {...}, "values": [[ts_ns, line], ...]}, ...]."""
    if not url:
        return [], "Loki is not configured (LOKI_URL unset)."
    now_ns = int(time.time() * 1_000_000_000)
    start_ns = now_ns - int(range_minutes * 60 * 1_000_000_000)
    try:
        resp = requests.get(
            f"{url}/loki/api/v1/query_range",
            params={
                "query": logql, "limit": str(limit),
                "start": str(start_ns), "end": str(now_ns), "direction": "backward",
            },
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return [], f"Loki returned HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json().get("data", {}).get("result", []), None
    except Exception as exc:  # pylint: disable=broad-except
        return [], f"Loki query failed: {exc}"


def loki_lines(streams: List[Dict[str, Any]], limit: int = 50) -> List[str]:
    """Flatten Loki query_range 'result' streams into '[pod] line' strings."""
    lines: List[str] = []
    for stream in streams:
        pod = stream.get("stream", {}).get("pod", "unknown")
        for _ts_ns, log_line in stream.get("values", []):
            lines.append(f"[{pod}] {log_line}")
    return lines[:limit]


def tempo_search_error_traces(url: str, service: str, minutes: int = 30, limit: int = 20) -> QueryResult:
    if not url:
        return [], "Tempo is not configured (TEMPO_URL unset)."
    try:
        now = time.time()
        traceql = f'{{resource.service.name="{service}" && status=error}}'
        resp = requests.get(
            f"{url}/api/search",
            params={"q": traceql, "start": int(now - minutes * 60), "end": int(now), "limit": limit},
            timeout=HTTP_TIMEOUT,
        )
        if resp.status_code != 200:
            return [], f"Tempo returned HTTP {resp.status_code}: {resp.text[:300]}"
        return resp.json().get("traces", []), None
    except Exception as exc:  # pylint: disable=broad-except
        return [], f"Tempo query failed: {exc}"


def tempo_fetch_trace(url: str, trace_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if not url:
        return None, "Tempo is not configured (TEMPO_URL unset)."
    try:
        resp = requests.get(f"{url}/api/traces/{trace_id}", timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None, f"Tempo returned HTTP {resp.status_code} for trace {trace_id}"
        return resp.json(), None
    except Exception as exc:  # pylint: disable=broad-except
        return None, f"Tempo query failed: {exc}"
