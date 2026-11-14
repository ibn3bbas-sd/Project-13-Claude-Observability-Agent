# Copyright 2026 Ibn Abbas
# SPDX-License-Identifier: Apache-2.0
"""
Sends Kubernetes context to Claude's API and parses structured analysis.

Claude gets an initial context dump (events, deployment spec, Loki error logs)
plus a bounded agentic tool-use loop — it can call query_prometheus, query_loki,
query_tempo_trace, kubectl_describe, and kubectl_get_events mid-investigation to
pull additional read-only evidence before committing to a final diagnosis.
"""

import json
import logging
import textwrap
from typing import Any, Dict, List, Optional

import anthropic
from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException

try:
    from .config import Config
    from .prepass import run_prepass
    from .outcome_store import load_outcomes, similar_outcomes
    from . import observability_clients as obs
except ImportError:
    from config import Config
    from prepass import run_prepass
    from outcome_store import load_outcomes, similar_outcomes
    import observability_clients as obs

logger = logging.getLogger(__name__)

MAX_TOOL_TURNS = 6  # bounds cost/latency of the investigation loop
FOLLOWUP_MAX_TOKENS = 1024

# Models where output_config.format (structured outputs) is available. On these the
# API constrains the final text block to schema-valid JSON, which removes the
# "Claude response could not be parsed as JSON" failure mode at the source rather
# than trying to repair it afterwards. claude-sonnet-4-6 is deliberately NOT in
# this set — structured outputs is unsupported there, so it keeps the prompt-only
# JSON contract plus the salvage path in _parse_response.
STRUCTURED_OUTPUT_MODEL_PREFIXES = (
    "claude-fable-5",
    "claude-mythos-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-haiku-4-5",
)

_NULLABLE_STRING = {"anyOf": [{"type": "string"}, {"type": "null"}]}

# Mirrors the JSON contract in ANALYSIS_PROMPT below. Structured outputs rejects
# length/numeric constraints, so the word limits stay prose-only in the prompt.
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "root_causes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "hypothesis": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence": {"type": "string"},
                },
                "required": ["hypothesis", "confidence", "evidence"],
                "additionalProperties": False,
            },
        },
        "remediation_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "priority": {"type": "integer"},
                    "description": {"type": "string"},
                    "kubectl_command": _NULLABLE_STRING,
                },
                "required": ["priority", "description", "kubectl_command"],
                "additionalProperties": False,
            },
        },
        "additional_checks": {"type": "array", "items": {"type": "string"}},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "summary": {"type": "string"},
    },
    "required": [
        "root_causes",
        "remediation_steps",
        "additional_checks",
        "severity",
        "summary",
    ],
    "additionalProperties": False,
}

# Mirrors the JSON contract in _build_followup_prompt.
FOLLOWUP_SCHEMA = {
    "type": "object",
    "properties": {
        "diagnosis": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "command": _NULLABLE_STRING,
                },
                "required": ["description", "command"],
                "additionalProperties": False,
            },
        },
        "note": _NULLABLE_STRING,
    },
    "required": ["diagnosis", "steps", "note"],
    "additionalProperties": False,
}

TOOLS = [
    {
        "name": "query_prometheus",
        "description": (
            "Run a PromQL query against the cluster's Prometheus. Use this to check "
            "metrics beyond what's already in the context above — a different time "
            "window, a different pod/namespace, or a signal not already surfaced. "
            "Known-good metric names on this cluster: "
            "kube_pod_container_status_restarts_total, container_memory_working_set_bytes, "
            "container_cpu_usage_seconds_total, kube_pod_container_status_last_terminated_reason "
            "(reason=\"OOMKilled\"), kube_pod_container_status_waiting_reason, "
            "kube_pod_status_unschedulable, kube_pod_status_reason (reason=\"Evicted\"), "
            "kube_pod_status_ready (condition=\"false\"), kube_persistentvolumeclaim_status_phase, "
            "kube_node_status_condition (condition=~\"DiskPressure|MemoryPressure|PIDPressure\")."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "promql": {"type": "string", "description": "PromQL expression."},
                "range_minutes": {
                    "type": "integer",
                    "description": "If set, run a range query over the last N minutes instead of an instant query.",
                },
            },
            "required": ["promql"],
        },
    },
    {
        "name": "query_loki",
        "description": (
            "Run a LogQL query against Loki for logs beyond the pre-fetched error logs "
            "above — e.g. a wider time window, a different pod pattern, or a dependency "
            "service's logs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "logql": {
                    "type": "string",
                    "description": 'LogQL query, e.g. \'{namespace="ns",pod=~"svc-.*"}\'',
                },
                "range_minutes": {"type": "integer", "description": "Lookback window in minutes (default 15)."},
            },
            "required": ["logql"],
        },
    },
    {
        "name": "query_tempo_trace",
        "description": "Fetch a specific trace by ID, or search Tempo for recent error traces for a service.",
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {"type": "string", "description": "Service name to search recent error traces for."},
                "trace_id": {"type": "string", "description": "Exact trace ID to fetch."},
            },
        },
    },
    {
        "name": "kubectl_describe",
        "description": (
            "Read-only describe of a Kubernetes object not already summarized above "
            "(e.g. a dependency Service/Ingress, or a Node to check pressure conditions)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "description": "pod|deployment|statefulset|service|ingress|pvc|node"},
                "name": {"type": "string"},
                "namespace": {"type": "string", "description": "Defaults to the service's namespace if omitted."},
            },
            "required": ["kind", "name"],
        },
    },
    {
        "name": "kubectl_get_events",
        "description": "Recent Kubernetes events for a namespace, optionally filtered to one object name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "namespace": {"type": "string", "description": "Defaults to the service's namespace if omitted."},
                "name": {"type": "string", "description": "Filter to events for this object name only."},
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------
ANALYSIS_PROMPT = textwrap.dedent(
    """
    You are a senior Kubernetes / Site Reliability Engineer.
    Analyze the following context from an unhealthy Kubernetes service in the
    **stage** environment and produce a JSON response ONLY — no prose outside the JSON.

    === SERVICE CONTEXT ===
    Service name   : {service_name}
    Namespace      : {namespace}
    Labels         : {labels}

    === HEALTH REPORT ===
    Desired replicas : {desired_replicas}
    Ready replicas   : {ready_replicas}
    Issues detected  : {issues}

    === POD STATUSES ===
    {pod_statuses}

    === KUBERNETES EVENTS (most recent first) ===
    {events}

    === DEPLOYMENT SPEC ===
    {deployment_spec}

    === ERROR LOGS (Loki — errors/exceptions only, last 1h) ===
    {log_context}

    === CORRELATED TIMELINE (Tempo error traces -> matching logs by trace ID -> metrics, last 30m) ===
    {correlated_timeline}

    === PRE-PASS HINTS (cheap deterministic checks — advisory, verify before trusting) ===
    {prepass_hints}

    === PAST INCIDENTS FOR THIS SERVICE (what was tried before, and whether it worked) ===
    {past_incidents}

    === TASK ===
    Diagnose the root cause and recommend concrete fix actions. You have read-only
    investigative tools available — query_prometheus, query_loki, query_tempo_trace,
    kubectl_describe, kubectl_get_events — use them if the evidence above is insufficient
    (e.g. a different time window, a dependency's health, an object not already shown).
    The correlated timeline above ties a trace to its own log lines and metrics by identity,
    not just overlapping timestamps — prefer it over the raw error logs when both cover the
    same request. If a trace has no correlated log lines, this app's logs may simply not carry
    trace IDs (unverified for this cluster) — that's not itself evidence of anything.
    Pre-pass hints above are cheap pattern matches, not confirmed diagnoses — treat them as
    a starting hypothesis to verify against the actual evidence, not something to restate
    uncritically. Past incidents show real outcomes on this exact service — prefer a command
    already recorded as RESOLVED for matching symptoms; be skeptical of re-suggesting one
    already recorded as NOT resolving this, unless you have new evidence it will work now.
    Only call a tool if it could change your diagnosis — don't call one just to confirm
    what's already shown above. Once confident, respond with ONLY the final JSON described
    below — no tool calls, no prose outside the JSON, in your last message.

    Return a JSON object with exactly these fields:
    {{
        "root_causes": [
            {{
                "hypothesis": "<10 words max>",
                "confidence": "high|medium|low",
                "evidence": "<15 words max — cite specific log line or metric>"
            }}
        ],
        "remediation_steps": [
            {{
                "priority": 1,
                "description": "<10 words max>",
                "kubectl_command": "<fix command only, or null>"
            }}
        ],
        "additional_checks": [
            "<10 words max>"
        ],
        "severity": "critical|high|medium|low",
        "summary": "<one sentence, 20 words max — state the actual problem found in logs/metrics>"
    }}

    Rules:
    - Respond with ONLY valid JSON. No markdown fences, no explanation outside the JSON.
    - Be extremely brief — every string field must respect the word limits above.
    - At most 2 root_causes, 3 remediation_steps, 2 additional_checks.
    - kubectl_command MUST be one of these supported actions ONLY:
        kubectl rollout restart deployment/<name> -n <ns>
        kubectl scale deployment/<name> --replicas=N -n <ns>
        kubectl delete pod <name> -n <ns>
        kubectl set image deployment/<name> <container>=<image> -n <ns>
        kubectl create service externalname <name> --external-name <fqdn> -n <ns>
        kubectl patch ingress <name> -n <ns> --type=json -p '[{{"op":"replace","path":"/spec/rules/0/host","value":"<new-unique-hostname>"}}]'
        kubectl patch service <name> -n <ns> --type=json -p '<json-patch>'
        kubectl patch deployment <name> -n <ns> --type=json -p '<json-patch>'
      NEVER put kubectl logs, kubectl exec, kubectl describe, or kubectl get here — those are
      investigative, not remediation; use the query_prometheus/query_loki/query_tempo_trace/
      kubectl_describe/kubectl_get_events tools instead if you need more evidence.
      NEVER use kubectl apply — it references files that don't exist in the cluster.
    - For Service targetPort mismatches (targetPort not found in pod containerPorts):
        Use kubectl patch service to correct the targetPort. Examples:
        Named port wrong: kubectl patch service <svc> -n <ns> --type=json -p '[{{"op":"replace","path":"/spec/ports/0/targetPort","value":"<correct-port-name>"}}]'
        Numeric port wrong: kubectl patch service <svc> -n <ns> --type=json -p '[{{"op":"replace","path":"/spec/ports/0/targetPort","value":<correct-port-number>}}]'
        Multiple ports: use index 0, 1, 2 etc. in the path for each port entry.
        Always use the ACTUAL port name/number exposed by the pod (from the issues text or deployment spec).
    - For deployment argument/env issues (e.g. invalid CLI flag like --auto-publish):
        Use kubectl patch deployment with strategic merge to replace the args list, removing the bad flag.
        Example — remove --auto-publish from a container that has args ["server", "--auto-publish", "--port=8080"]:
        kubectl patch deployment <name> -n <ns> --type=strategic -p '{{"spec":{{"template":{{"spec":{{"containers":[{{"name":"<container>","args":["server","--port=8080"]}}]}}}}}}}}'
        List only the REMAINING valid args (all args except the bad one). Use --type=strategic (not json) so
        only the named container is affected. Set container name from the deployment spec above.
    - For duplicate/conflicting ingress hostnames: use kubectl patch ingress to assign a unique
      hostname to the conflicting ingress. Fill in the exact ingress name, namespace, and a
      unique hostname derived from the service name (e.g. <svc-name>-stg.example.com).
    - For a missing ingress backend service: first check the issue text for "existing services
      with similar names". If a similar-named service already exists in the namespace, the fix
      is to patch the ingress backend name — set kubectl_command to null and explain in
      description that the ingress backend name should be corrected to the existing service name.
      Only suggest ExternalName if no similar local service exists.
    - If none of the above commands can fix the issue, set kubectl_command to null.
    - evidence in root_causes must reference actual log text or metric values from the context above,
      not generic descriptions.
    - If the logs show a specific error (e.g. OOMKilled, connection refused, config missing), name it.
    """
).strip()


class ClaudeAnalyzer:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # The client reads ANTHROPIC_API_KEY automatically from the environment
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        # kubernetes config was already loaded by HealthChecker.__init__
        self.core_v1 = k8s_client.CoreV1Api()
        self.apps_v1 = k8s_client.AppsV1Api()
        self.networking_v1 = k8s_client.NetworkingV1Api()

    def analyze_with_claude(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Send context to Claude and return a parsed analysis dict.
        Gives Claude a bounded tool-use loop (see TOOLS) to pull additional
        read-only evidence before it commits to a final diagnosis.
        Falls back to a minimal error dict on failure.
        """
        ns = context["namespace"]
        svc_name = context["service_name"]
        messages: List[Dict[str, Any]] = [{"role": "user", "content": self._build_prompt(context)}]
        logger.info("Sending context for %s/%s to Claude (%s)", ns, svc_name, self.cfg.claude_model)

        output_config = self._output_config(ANALYSIS_SCHEMA)

        try:
            for turn in range(MAX_TOOL_TURNS):
                response = self.client.messages.create(
                    model=self.cfg.claude_model,
                    max_tokens=self.cfg.claude_max_tokens,
                    tools=TOOLS,
                    messages=messages,
                    **({"output_config": output_config} if output_config else {}),
                )

                if response.stop_reason != "tool_use":
                    truncated = response.stop_reason == "max_tokens"
                    if truncated:
                        logger.warning(
                            "Claude response truncated (max_tokens=%d) — increase CLAUDE_MAX_TOKENS",
                            self.cfg.claude_max_tokens,
                        )
                    raw_text = self._text_of(response)
                    analysis = self._parse_response(raw_text, truncated=truncated)
                    logger.info(
                        "Claude analysis complete for %s — severity: %s (tool turns used: %d)",
                        svc_name, analysis.get("severity", "unknown"), turn,
                    )
                    return analysis

                messages.append({"role": "assistant", "content": response.content})
                tool_results = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    logger.info("Claude tool call for %s/%s: %s(%s)", ns, svc_name, block.name, block.input)
                    result_text = self._dispatch_tool(block.name, block.input, default_namespace=ns)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    })
                messages.append({"role": "user", "content": tool_results})

            logger.warning(
                "Claude tool loop exceeded %d turns without a final answer for %s/%s",
                MAX_TOOL_TURNS, ns, svc_name,
            )
            return self._error_analysis(f"Investigation exceeded {MAX_TOOL_TURNS} tool-use turns without concluding.")

        except anthropic.APIError as exc:
            logger.error("Claude API error: %s", exc)
            return self._error_analysis(str(exc))
        except Exception as exc:  # pylint: disable=broad-except
            logger.error("Unexpected error calling Claude: %s", exc, exc_info=True)
            return self._error_analysis(str(exc))

    def _output_config(self, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Structured-output config, or None on models that don't support it."""
        if not self.cfg.claude_model.startswith(STRUCTURED_OUTPUT_MODEL_PREFIXES):
            return None
        return {"format": {"type": "json_schema", "schema": schema}}

    @staticmethod
    def _text_of(response: Any) -> str:
        """Concatenate the text blocks of a response.

        Never index content[0] — a response may lead with a thinking or tool_use
        block, in which case .text doesn't exist on it.
        """
        return "".join(
            b.text for b in response.content if getattr(b, "type", None) == "text"
        ).strip()

    # ------------------------------------------------------------------
    # Tool dispatch (read-only investigative tools — see TOOLS above)
    # ------------------------------------------------------------------
    def _dispatch_tool(self, name: str, tool_input: Dict[str, Any], default_namespace: str) -> str:
        try:
            if name == "query_prometheus":
                return self._query_prometheus(tool_input.get("promql", ""), tool_input.get("range_minutes"))
            if name == "query_loki":
                return self._query_loki(tool_input.get("logql", ""), tool_input.get("range_minutes"))
            if name == "query_tempo_trace":
                return self._query_tempo(tool_input.get("service"), tool_input.get("trace_id"))
            if name == "kubectl_describe":
                return self._kubectl_describe(
                    tool_input.get("kind", ""),
                    tool_input.get("name", ""),
                    tool_input.get("namespace") or default_namespace,
                )
            if name == "kubectl_get_events":
                return self._kubectl_get_events(tool_input.get("namespace") or default_namespace, tool_input.get("name"))
            return f"Unknown tool '{name}'."
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning("Tool '%s' dispatch failed: %s", name, exc, exc_info=True)
            return f"Tool '{name}' failed: {exc}"

    def _query_prometheus(self, promql: str, range_minutes: Optional[int]) -> str:
        if not promql:
            return "query_prometheus requires 'promql'."
        if range_minutes:
            result, error = obs.prometheus_query_range(self.cfg.prometheus_url, promql, range_minutes)
        else:
            result, error = obs.prometheus_query(self.cfg.prometheus_url, promql)
        if error:
            return error
        return json.dumps(result[:20], default=str)[:3000] if result else "(empty result)"

    def _query_loki(self, logql: str, range_minutes: Optional[int]) -> str:
        if not logql:
            return "query_loki requires 'logql'."
        streams, error = obs.loki_query_range(self.cfg.loki_url, logql, range_minutes or 15)
        if error:
            return error
        lines = obs.loki_lines(streams, limit=50)
        return "\n".join(lines) if lines else "(no matching logs)"

    def _query_tempo(self, service: Optional[str], trace_id: Optional[str]) -> str:
        if trace_id:
            trace, error = obs.tempo_fetch_trace(self.cfg.tempo_url, trace_id)
            if error:
                return error
            return json.dumps(trace, default=str)[:3000]
        if not service:
            return "query_tempo_trace requires 'service' or 'trace_id'."
        traces, error = obs.tempo_search_error_traces(self.cfg.tempo_url, service, minutes=30, limit=20)
        if error:
            return error
        summary = [
            {
                "traceID": t.get("traceID"),
                "rootService": t.get("rootServiceName"),
                "rootName": t.get("rootTraceName"),
                "durationMs": t.get("durationMs"),
            }
            for t in traces[:20]
        ]
        return json.dumps(summary) if summary else "(no recent error traces found)"

    def _kubectl_describe(self, kind: str, name: str, namespace: str) -> str:
        if not kind or not name:
            return "kubectl_describe requires 'kind' and 'name'."
        kind_l = kind.lower()
        try:
            if kind_l in ("pod", "pods", "po"):
                obj = self.core_v1.read_namespaced_pod(name, namespace)
            elif kind_l in ("deployment", "deployments", "deploy"):
                obj = self.apps_v1.read_namespaced_deployment(name, namespace)
            elif kind_l in ("statefulset", "statefulsets", "sts"):
                obj = self.apps_v1.read_namespaced_stateful_set(name, namespace)
            elif kind_l in ("service", "services", "svc"):
                obj = self.core_v1.read_namespaced_service(name, namespace)
            elif kind_l in ("ingress", "ingresses", "ing"):
                obj = self.networking_v1.read_namespaced_ingress(name, namespace)
            elif kind_l in ("persistentvolumeclaim", "persistentvolumeclaims", "pvc"):
                obj = self.core_v1.read_namespaced_persistent_volume_claim(name, namespace)
            elif kind_l in ("node", "nodes"):
                obj = self.core_v1.read_node(name)
            else:
                return f"kubectl_describe: unsupported kind '{kind}'. Supported: pod, deployment, statefulset, service, ingress, pvc, node."
        except ApiException as exc:
            return f"kubectl_describe {kind}/{name} failed ({exc.status}): {exc.reason}"
        data = k8s_client.ApiClient().sanitize_for_serialization(obj)
        if isinstance(data, dict):
            data.get("metadata", {}).pop("managedFields", None)
        return json.dumps(data, default=str)[:4000]

    def _kubectl_get_events(self, namespace: str, name: Optional[str]) -> str:
        try:
            if name:
                raw = self.core_v1.list_namespaced_event(namespace, field_selector=f"involvedObject.name={name}")
            else:
                raw = self.core_v1.list_namespaced_event(namespace)
        except ApiException as exc:
            return f"kubectl_get_events failed ({exc.status}): {exc.reason}"

        def _sort_key(ev: Any) -> str:
            ts = ev.last_timestamp or ev.event_time or ev.first_timestamp
            return str(ts) if ts else ""

        events = sorted(raw.items, key=_sort_key, reverse=True)[:30]
        lines = [
            f"{_sort_key(e)} {e.type} {e.reason} {e.involved_object.kind}/{e.involved_object.name}: {e.message}"
            for e in events
        ]
        return "\n".join(lines) if lines else "(no events)"

    def analyze_followup(
        self,
        original_context: Dict[str, Any],
        original_analysis: Dict[str, Any],
        reply_text: str,
        reply_author: str,
    ) -> Dict[str, Any]:
        """Return a structured dict: {diagnosis, steps:[{description,command}], note}."""
        prompt = self._build_followup_prompt(
            original_context, original_analysis, reply_text, reply_author,
        )
        logger.info(
            "Sending follow-up for %s/%s to Claude (reply from %s)",
            original_context["namespace"],
            original_context["service_name"],
            reply_author,
        )
        output_config = self._output_config(FOLLOWUP_SCHEMA)
        try:
            response = self.client.messages.create(
                model=self.cfg.claude_model,
                max_tokens=FOLLOWUP_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
                **({"output_config": output_config} if output_config else {}),
            )
            raw = self._text_of(response)
            logger.info("Claude follow-up response received (%d chars)", len(raw))
            parsed = self._parse_response(
                raw, truncated=response.stop_reason == "max_tokens",
            )
            if "diagnosis" not in parsed:
                return {
                    "diagnosis": parsed.get("summary", "Follow-up could not be parsed."),
                    "steps": [],
                    "note": None,
                }
            return parsed
        except Exception as exc:
            logger.error("Claude follow-up failed: %s", exc, exc_info=True)
            return {"diagnosis": f"Follow-up unavailable: {exc}", "steps": [], "note": None}

    def _build_followup_prompt(
        self,
        context: Dict[str, Any],
        analysis: Dict[str, Any],
        reply_text: str,
        reply_author: str,
    ) -> str:
        return textwrap.dedent(f"""\
            You are a senior Kubernetes SRE responding in a Teams thread.

            === ALERT CONTEXT ===
            Service: {context["service_name"]} in {context["namespace"]}
            Issues: {"; ".join(context["health_report"].get("issues", []))}
            Summary: {analysis.get("summary", "N/A")}
            Root causes: {json.dumps(analysis.get("root_causes", []))}

            === ENGINEER'S MESSAGE ===
            From: {reply_author}
            Message: {reply_text}

            === TASK ===
            Reply with ONLY valid JSON — no prose outside the JSON:
            {{
                "diagnosis": "<one sentence, 20 words max>",
                "steps": [
                    {{
                        "description": "<10 words max>",
                        "command": "<exact kubectl/helm command, or null>"
                    }}
                ],
                "note": "<optional caution or follow-up, 15 words max, or null>"
            }}
            Rules:
            - At most 3 steps.
            - kubectl commands must use exact namespace and resource names from context.
            - command field is null if the step has no CLI command.
            - note is null if there is nothing important to add.""")

    # ------------------------------------------------------------------
    def _build_prompt(self, context: Dict[str, Any]) -> str:
        hr = context["health_report"]

        hints = run_prepass(context)
        if hints:
            logger.info(
                "Pre-pass matched %d hint(s) for %s/%s",
                len(hints), context["namespace"], context["service_name"],
            )
        prepass_hints_str = "\n".join(f"- {h}" for h in hints) if hints else "(none matched)"

        past = similar_outcomes(
            load_outcomes(self.core_v1, self.cfg.agent_namespace),
            context["service_name"], context["namespace"],
        )
        if past:
            past_lines = []
            for o in past:
                status = "RESOLVED" if o.get("resolved") else "did NOT resolve"
                ttr = o.get("ttr_seconds")
                ttr_str = f", took {ttr}s" if ttr is not None else ""
                past_lines.append(
                    f"- {o.get('timestamp', '?')}: tried `{o.get('command')}` for "
                    f"\"{o.get('hypothesis', '?')}\" — {status}{ttr_str}"
                )
            past_incidents_str = "\n".join(past_lines)
        else:
            past_incidents_str = "(no past incidents recorded for this service)"

        correlated_timeline_str = self._format_correlated_timeline(context.get("correlated_timeline") or {})

        events_str = json.dumps(context.get("events", []), indent=2)
        pod_statuses_str = json.dumps(hr.get("pod_statuses", []), indent=2)
        deployment_spec_str = json.dumps(context.get("deployment_spec", {}), indent=2)

        loki_logs = context.get("loki_logs", "")
        if loki_logs:
            log_context = loki_logs
        else:
            pod_logs_parts = []
            for pod_name, logs in context.get("pod_logs", {}).items():
                truncated = "\n".join(logs.splitlines()[-self.cfg.max_log_lines:])
                pod_logs_parts.append(f"--- {pod_name} ---\n{truncated}")
            log_context = "\n\n".join(pod_logs_parts) if pod_logs_parts else "(no error logs)"

        return ANALYSIS_PROMPT.format(
            service_name=context["service_name"],
            namespace=context["namespace"],
            labels=json.dumps(context.get("labels", {})),
            desired_replicas=hr.get("desired_replicas", "?"),
            ready_replicas=hr.get("ready_replicas", "?"),
            issues="; ".join(hr.get("issues", [])) or "none listed",
            pod_statuses=pod_statuses_str,
            events=events_str,
            deployment_spec=deployment_spec_str,
            prepass_hints=prepass_hints_str,
            past_incidents=past_incidents_str,
            log_context=log_context,
            correlated_timeline=correlated_timeline_str,
        )

    @staticmethod
    def _format_correlated_timeline(timeline: Dict[str, Any]) -> str:
        traces = timeline.get("traces") or []
        if not traces:
            return "(no recent error traces found — Tempo not configured, no errors, or none yet)"
        correlated_logs = timeline.get("correlated_logs") or {}
        lines = []
        for t in traces:
            trace_id = t.get("traceID")
            lines.append(f"- trace {trace_id}: {t.get('rootName')} ({t.get('durationMs')}ms)")
            trace_logs = correlated_logs.get(trace_id)
            if trace_logs:
                lines.append(f"  correlated logs ({len(trace_logs)}):")
                for log_line in trace_logs[:5]:
                    lines.append(f"    {log_line}")
            else:
                lines.append("  (no log lines matched this trace ID)")
        metrics = timeline.get("metric_series") or {}
        if metrics:
            lines.append(f"Metrics for the same window: {json.dumps(metrics, default=str)[:1000]}")
        return "\n".join(lines)

    @staticmethod
    def _parse_response(raw: str, truncated: bool = False) -> Dict[str, Any]:
        """Parse the model's final message into the analysis dict.

        On models without structured outputs the response can carry a prose
        preamble, markdown fences, or a trailing sentence around the JSON — plain
        json.loads() rejects all three and the alert then reaches Teams with no
        severity and no remediation command. Salvage in escalating order, and if
        every attempt fails, carry the raw text through so the card can show what
        the model actually said instead of only that parsing failed.
        """
        candidates = []
        if raw:
            candidates.append(raw)

        # Fenced block: ```json ... ``` — take the fenced body, not the whole message
        if "```" in raw:
            stripped = "\n".join(
                ln for ln in raw.splitlines() if not ln.lstrip().startswith("```")
            ).strip()
            if stripped:
                candidates.append(stripped)

        # Outermost brace span: drops any prose before/after the object
        start, end = raw.find("{"), raw.rfind("}")
        if start != -1 and end > start:
            candidates.append(raw[start:end + 1])

        # Truncated mid-object (hit max_tokens): close what's open and retry, so a
        # cut-off response still yields the fields that did arrive.
        if truncated and start != -1:
            candidates.append(ClaudeAnalyzer._close_truncated_json(raw[start:]))

        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            # An empty dict is what the truncation repair yields from a fragment that
            # never got past its opening brace — worse than the degraded fallback below,
            # which at least says why the analysis is missing.
            if isinstance(parsed, dict) and parsed:
                return parsed

        logger.warning(
            "Failed to parse Claude JSON response after %d attempt(s)%s. Raw: %s",
            len(candidates),
            " (response was truncated at max_tokens)" if truncated else "",
            raw[:2000] or "(empty)",
        )
        return {
            "root_causes": [],
            "remediation_steps": [],
            "additional_checks": [
                "Increase CLAUDE_MAX_TOKENS — response was cut off."
                if truncated
                else "Check agent logs for the raw Claude response."
            ],
            "severity": "unknown",
            "summary": (
                "Claude response was truncated before valid JSON (raise CLAUDE_MAX_TOKENS)."
                if truncated
                else "Claude response could not be parsed as JSON — see raw text below."
            ),
            "_raw": raw,
        }

    @staticmethod
    def _close_truncated_json(fragment: str) -> str:
        """Best-effort close of a JSON object cut off mid-serialisation."""
        # Drop a dangling partial string value
        if fragment.count('"') % 2:
            fragment = fragment[:fragment.rfind('"')]
        fragment = fragment.rstrip()

        # Peel trailing separators, plus any key left with no value — "summary":
        # becomes "summary" once the colon goes, and a bare key can't be closed.
        while fragment:
            if fragment[-1] in ',:':
                fragment = fragment[:-1].rstrip()
                continue
            if fragment[-1] == '"':
                opening = ClaudeAnalyzer._string_start(fragment)
                if opening is not None:
                    before = fragment[:opening].rstrip()
                    if before and before[-1] in '{,':
                        fragment = before
                        continue
            break

        closers = []
        in_string = False
        escaped = False
        for ch in fragment:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = not in_string
            elif not in_string:
                if ch == "{":
                    closers.append("}")
                elif ch == "[":
                    closers.append("]")
                elif ch in "}]" and closers:
                    closers.pop()
        return fragment + "".join(reversed(closers))

    @staticmethod
    def _string_start(fragment: str) -> Optional[int]:
        """Index of the opening quote of the JSON string ending at fragment[-1]."""
        i = len(fragment) - 2
        while i >= 0:
            if fragment[i] == '"':
                backslashes = 0
                j = i - 1
                while j >= 0 and fragment[j] == "\\":
                    backslashes += 1
                    j -= 1
                if backslashes % 2 == 0:
                    return i
            i -= 1
        return None

    @staticmethod
    def _error_analysis(error_message: str) -> Dict[str, Any]:
        return {
            "root_causes": [],
            "remediation_steps": [],
            "additional_checks": ["Check monitoring agent logs for Claude API errors."],
            "severity": "unknown",
            "summary": f"LLM analysis unavailable: {error_message}",
        }
