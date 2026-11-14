# Claude Observability Agent — Enhancement Roadmap

Research memo comparing our agent's architecture against HolmesGPT, kagent, Datadog Bits AI SRE,
Dynatrace Davis AI, Grafana Sift, and PagerDuty's SRE Agent, with a prioritized upgrade path.

**Prepared:** 2026-07-02

> **Confidence note:** the research run hit its session token limit before every claim finished
> adversarial re-verification. Claims marked `[verified]` passed a 3-vote independent check against
> primary sources. Claims marked `[sourced]` come from a primary source (official docs/eng blogs) but
> didn't finish re-verification. The safety gap below was found by reading `monitor.py` directly, not
> from external research.

---

## Executive summary

The agent's core limitation is structural: `agent/claude_analyzer.py` makes **one** Claude API call per
incident with context fetched **before** Claude sees it. Every serious competitor has moved past this
shape — Datadog's Bits AI SRE runs a hypothesis-generate-and-validate loop; HolmesGPT and kagent give the
model a live tool-use loop against 50+ real data sources; Dynatrace (no LLM loop at all) still insists
naive time-window correlation across signals produces false positives without a dependency model behind it.

The single highest-leverage change: give Claude an **agentic tool-use loop** (Anthropic's `tool_use`
primitive) so it can call `query_prometheus`, `query_loki`, `query_tempo_trace`, `kubectl_describe`, and
`kubectl_get_events` mid-reasoning — the same pattern HolmesGPT uses — instead of a one-shot prompt that
hopes the pre-fetched context was enough. Everything else (correlation, safety, feedback loops) builds on
top of that change.

Separately, reading `monitor.py` surfaced a **live safety gap** independent of the AI architecture:
ingress remediation auto-executes its top suggested `kubectl` command before any human sees it, while
service/deployment remediation correctly waits for a Teams reply first.

---

## Landscape comparison

| System | Investigation model | Signal correlation | Remediation safety | Adopt for us? |
|---|---|---|---|---|
| **Our agent (today)** | Single Claude call, context pre-fetched once. No mid-reasoning tool access. | None — Loki logs go in; Prometheus/Tempo data never reaches Claude. | ~10 whitelisted `kubectl` shapes enforced by prompt convention, run through a custom K8s-client executor. | — |
| **HolmesGPT** (Robusta, CNCF Sandbox) | True agentic loop — queries live data across 50–60+ toolsets mid-investigation. `[verified]` | Agent decides what to pull: Prometheus, Grafana, Loki, Datadog, New Relic, K8s/OpenShift/AKS, 8+ databases, CI/CD. `[verified]` | Read-only + RBAC by default. Separate opt-in "Kubernetes Remediation" toolset: 4 auto-exec read-only tools + 1 mutating tool gated by approval, verb allowlist, dangerous-flag blocklist. "Operator Mode" opens a GitHub PR instead of patching live objects. `[verified]`/`[sourced]` | **High** — toolset shape and Operator Mode PR pattern map almost directly onto our stack. |
| **kagent** (CNCF Sandbox, Solo.io) | Planner + specialist sub-agents over MCP-style tools; declarative agent framework. `[sourced]` | Tools cover pod logs, Prometheus queries, resource generation. `[sourced]` | `requireApproval` — declarative per-tool flag gating only mutating tools behind an Approve/Reject UI; read-only tools run unattended. `[sourced]` | **High** — cleanest model for replacing our string-matched whitelist. |
| **Datadog Bits AI SRE** | Autonomous hypothesis loop: generates multiple root-cause theories, validates/invalidates each via targeted queries, repeats until converged. `[verified]` | Reasons across metrics, logs, traces, infra metadata, network telemetry, monitor config, APM, RUM, deploys, K8s events. `[verified]` | Not disclosed in verified sources. | **Medium** — hypothesis-validate loop shape worth copying; their eval work found failures came from *combining* tool outputs, not individual tool accuracy — test the full loop, not tools in isolation. `[verified]` |
| **Dynatrace Davis AI** | Not an LLM loop — deterministic topology-graph (Smartscape) + fault-tree analysis over a pre-built dependency model. `[verified]` | Structural/dependency correlation. Explicitly states "time correlation alone is not sufficient." `[verified]` | N/A — detection/RCA layer only. | **Conceptual only** — shapes how we build correlation (P0-2 below). |
| **Grafana Sift** | Fixed checklist run per alert — new log error patterns, recent deploys, overloaded nodes, HTTP error spikes, SLO burn. Not agentic reasoning. `[verified]` | Pulls from Loki/Prometheus/deployment events per check, not agent-directed. | N/A | **Low-effort win** — cheap "always run these checks first" pre-pass before the expensive Claude call (P1). |
| **PagerDuty SRE Agent** | Reported: supervisor spawns parallel specialist sub-agents, each testing a distinct hypothesis, fanning in asynchronously. `[sourced, re-verification incomplete]` | Reported: logs, metrics, deployments, topology/dependency graph, historical incidents. | Not covered in available source material. | **Later** — highest-effort pattern; revisit once a single-agent loop is proven (P2). |

---

## Prioritized roadmap

### P0 — foundational, do first

> **Status (2026-07-09): all three P0 items done.** Implemented in order 2, 3, then
> 1 (tool-use loop in `agent/claude_analyzer.py`, live Prometheus/Loki/Tempo
> query tools, `kubectl_describe`/`kubectl_get_events`; RBAC extended for pvc/node reads).

**1. Give Claude an agentic tool-use loop**
Replace the single `self.client.messages.create(...)` call in `analyze_with_claude` with a bounded
while-loop that lets Claude call `query_prometheus`, `query_loki`, `query_tempo_trace`,
`kubectl_describe`, `kubectl_get_events` mid-reasoning, executes them read-only, and feeds results back —
same shape as HolmesGPT / Bits AI SRE.
Touches: `agent/claude_analyzer.py` (new loop + tool schemas), `agent/monitor.py` (wire live
Prometheus/Loki/Tempo clients into the analyzer instead of only `ContextCollector`'s one-shot output).

**2. Fix the ingress auto-execute safety gap**
In `run_health_check`, service/deployment remediation waits for a human Teams reply before running any
`kubectl` command. Ingress remediation does not — it runs the top-ranked `remediation_steps` command
immediately. Finding: `agent/monitor.py`, ingress loop (~line 733–753): `auto_cmd` is computed from
`analysis["remediation_steps"]` and executed via `CommandExecutor().execute(auto_cmd)` unconditionally
when one exists — no approval gate, no dry-run. This is the one place in the codebase where an
LLM-suggested mutating command runs without a human in the loop.
Touches: `agent/monitor.py` (ingress remediation branch).

**3. Replace the string-matched kubectl whitelist with a policy registry**
The "whitelist" today lives as prose inside `ANALYSIS_PROMPT` — nothing in `CommandExecutor` enforces a
policy in code. Borrow kagent's `requireApproval` idea and HolmesGPT's tiered read-only/mutating split: a
small Python registry mapping each action to a blast-radius tier, whether it auto-runs, and whether it
dry-runs first.
Touches: `agent/monitor.py` (`CommandExecutor`), new small policy module.

### P1 — high value, builds on P0

> **Status (2026-07-09): all three P1 items done** (implemented in order 6, 5, 4).

**4. Correlate signals by identity, not just timestamp** — pull recent error traces for the affected
service from Tempo, extract trace IDs, pull Loki log lines carrying those trace IDs (requires trace-ID
correlation in logs — verify current Promtail/OTel config supports this), pull a Prometheus range query
for the same pod/window. One coherent timeline instead of three disjoint blobs.
Touches: `agent/context_collector.py` (new `build_correlated_timeline()`).
> **Status (2026-07-09): done.** `ContextCollector.build_correlated_timeline()` — Tempo error
> traces (top 5) -> Loki filtered per trace ID -> Prometheus CPU range query, same ~30 min window.
> Fed into the prompt as `=== CORRELATED TIMELINE ===`. Extracted the Prometheus/Loki/Tempo HTTP
> calls (previously duplicated between this and `claude_analyzer.py`'s on-demand tools, added for
> P0-1) into a new shared `agent/observability_clients.py` — both now call the same functions.
> Degrades gracefully at every step, including the still-**unverified** assumption that app logs
> actually carry trace IDs — if they don't, correlated_logs for a trace is just empty and the
> prompt says so explicitly, it's not treated as a failure.

**5. Persist outcomes and reuse them** — `run_health_check` already re-checks health after a mutating
command and knows whether it worked; that signal is discarded today. Log it (service, hypothesis,
command, resolved, time-to-resolve) to a small store, then retrieve similar past incidents as grounding
context before the next Claude call. Minimal viable RAG + eval dataset in one.
Touches: `agent/monitor.py` (log outcome after existing re-check), `agent/claude_analyzer.py` (inject
retrieved incidents into prompt).
> **Status (2026-07-09): done.** New `agent/outcome_store.py` — ConfigMap-backed
> (`claude-agent-outcomes`, same pattern as the alert-cooldown ConfigMap), bounded to 50 records.
> Written from `monitor.py`'s service/deployment reply handler only (ingress has no post-check to
> source a `resolved` signal from). Read from `ClaudeAnalyzer._build_prompt` — exact
> service+namespace match, most-recent-first, no embeddings (genuinely "minimal viable RAG", not
> a fuzzy retrieval). Eval-dataset half of this item (systematically scoring outcomes) not built —
> the ConfigMap is an append-only log usable for that later, but nothing consumes it that way yet.

**6. Cheap deterministic pre-pass before the expensive call** — Grafana Sift-style fixed checklist: recent
deploy in last 10 min? node under memory/disk pressure? OOMKilled in last hour? matches a known signature
already in this project's history (Rancher operator conflict, WAL death spiral)? Nearly free, often
answers the question before Claude is invoked.
Touches: `agent/health_checker.py` or new pre-pass module, feeding hints into `agent/claude_analyzer.py`.
> **Status (2026-07-09): done.** New `agent/prepass.py::run_prepass()` — OOMKilled (including via
> `last_state`, a real gap fixed in `health_checker.py` in the same change), Evicted, recent
> Deployment rollout (10 min window), and known signatures for `martin --auto-publish` / Redis
> `cluster_state:fail`. Deliberately excludes the Rancher-operator/WAL signatures — those are
> specific to the Prometheus StatefulSet in `observability`, outside this agent's monitored
> namespaces. Called from `ClaudeAnalyzer._build_prompt`, not `monitor.py` or `health_checker.py`.

### P2 — bigger investment

> **Status (2026-07-09):** 8 → done (opt-in/unverified against a real repo). 7 → not
> started, correctly, per its own stated precondition.

**7. Parallel hypothesis fan-out** for genuinely ambiguous incidents (PagerDuty-style supervisor + parallel
sub-agents). Depends on the P0 tool loop already being reliable — revisit only after single-agent
investigation quality is measured and found wanting.
> Status: not started — intentionally, per this item's own stated precondition above. Single-agent
> investigation quality hasn't been measured yet.

**8. GitOps-first remediation for Helm-owned drift** — this project's own `CLAUDE.md` already documents
Helm reverting manual `kubectl` patches on ingress hostname conflicts, worked around today with an alert
cooldown rather than a real fix. HolmesGPT's "Operator Mode" (open a PR against chart values instead of
patching the live object) is the correct fix for this recurring class of issue.
> **Status (2026-07-09): done, opt-in.** New `agent/gitops_remediation.py::open_hostname_pr()` —
> when a `GITOPS_REPO_MAP` entry exists for an ingress and Claude suggests a hostname-replace patch,
> opens a PR against the owning chart's values file (idempotent — checks for an already-open PR
> before creating a new one) instead of just re-alerting every cooldown cycle. With no mapping
> configured, this is a complete no-op — there's no way to infer which repo/chart owns an arbitrary
> monitored service's ingress from inside this repo, so nothing is guessed. Teams card shows the PR
> link instead of the raw kubectl command when a PR was opened (`agent/teams_notifier.py`).
> Not yet exercised against a real repo/token — verified via mocked GitHub API responses only
> (branch creation, file update, PR creation, and the idempotent-existing-PR path all covered).

---

## Implementation sketches (P0 items)

### 1. Agentic tool-use loop

```python
# agent/claude_analyzer.py
TOOLS = [
    {"name": "query_prometheus", "description": "Run a PromQL instant/range query.",
     "input_schema": {"type": "object", "properties": {
         "promql": {"type": "string"}, "range_minutes": {"type": "integer"}}}},
    {"name": "query_loki", "description": "Run a LogQL query against the last N minutes.",
     "input_schema": {"type": "object", "properties": {
         "logql": {"type": "string"}, "range_minutes": {"type": "integer"}}}},
    {"name": "query_tempo_trace", "description": "Fetch a trace or find recent error traces for a service.",
     "input_schema": {"type": "object", "properties": {
         "service": {"type": "string"}, "trace_id": {"type": "string"}}}},
    {"name": "kubectl_describe", "description": "Read-only describe of a k8s object.",
     "input_schema": {"type": "object", "properties": {
         "kind": {"type": "string"}, "name": {"type": "string"}, "namespace": {"type": "string"}}}},
    {"name": "kubectl_get_events", "description": "Recent events for a namespace.",
     "input_schema": {"type": "object", "properties": {"namespace": {"type": "string"}}}},
]

def analyze_with_claude(self, context):
    messages = [{"role": "user", "content": self._build_prompt(context)}]
    for _ in range(MAX_TOOL_TURNS):   # e.g. 8 — bound cost/latency
        resp = self.client.messages.create(
            model=self.cfg.claude_model, max_tokens=self.cfg.claude_max_tokens,
            tools=TOOLS, messages=messages,
        )
        if resp.stop_reason != "tool_use":
            return self._parse_response(resp.content[0].text)
        messages.append({"role": "assistant", "content": resp.content})
        results = [
            {"type": "tool_result", "tool_use_id": b.id,
             "content": self._dispatch_tool(b.name, b.input)}   # read-only only
            for b in resp.content if b.type == "tool_use"
        ]
        messages.append({"role": "user", "content": results})
    return self._error_analysis("tool loop exceeded max turns")
```

### 2. Identity-aware signal correlation

```python
# agent/context_collector.py
def build_correlated_timeline(self, svc, window_minutes=15):
    traces = self.tempo.search_errors(service=svc["name"], minutes=window_minutes)
    trace_ids = [t["traceID"] for t in traces]
    logs_by_trace = {
        tid: self.loki.query(f'{{namespace="{svc["namespace"]}"}} |= "{tid}"')
        for tid in trace_ids
    }   # requires trace_id present in log lines — verify Promtail/OTel log pipeline does this
    metrics = self.prom.query_range(
        f'rate(container_cpu_usage_seconds_total{{pod=~"{svc["name"]}.*"}}[2m])',
        minutes=window_minutes,
    )
    return {"traces": traces, "correlated_logs": logs_by_trace, "metric_series": metrics}
```

### 3. Policy-based remediation safety

```python
# agent/monitor.py — new policy layer around CommandExecutor
ACTION_POLICY = {
    "rollout_restart":             {"tier": 1, "auto": True},
    "delete_pod":                  {"tier": 1, "auto": True},
    "scale":                       {"tier": 1, "auto": True},
    "set_image":                   {"tier": 2, "auto": False, "dry_run": True},
    "patch_deployment":            {"tier": 2, "auto": False, "dry_run": True},
    "patch_ingress":                {"tier": 2, "auto": False, "dry_run": True},  # was: auto-executed
    "patch_service":               {"tier": 2, "auto": False, "dry_run": True},
    "create_service_externalname": {"tier": 2, "auto": False},
}

def execute_with_policy(action, args, executor, notifier, message_id):
    policy = ACTION_POLICY[action]
    if policy.get("dry_run"):
        preview = executor.dispatch(action, args, dry_run="All")  # native k8s API dry-run
        if not policy["auto"]:
            notifier.post_dry_run_card(message_id, action, preview)
            return  # wait for the same Teams-reply approval path services already use
    return executor.dispatch(action, args)
```

---

## Sources

- Datadog — [Bits AI SRE](https://www.datadoghq.com/blog/bits-ai-sre/), [Bits AI eval platform](https://www.datadoghq.com/blog/engineering/bits-ai-eval-platform/)
- Dynatrace — [Davis AI root cause analysis concepts](https://docs.dynatrace.com/docs/dynatrace-intelligence/root-cause-analysis/concepts)
- Grafana Labs — [AI-powered diagnostics with Sift](https://grafana.com/blog/2024/02/21/ai-powered-diagnostics-for-incident-response-new-sift-features-in-grafana-irm/)
- Anthropic — [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents), [Tool use overview](https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview), [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp)
- HolmesGPT (Robusta / CNCF Sandbox) — [GitHub](https://github.com/HolmesGPT/holmesgpt), [Kubernetes Remediation toolset docs](https://holmesgpt.dev/latest/data-sources/builtin-toolsets/kubernetes-remediation-mcp/), [CNCF blog](https://www.cncf.io/blog/2026/01/07/holmesgpt-agentic-troubleshooting-built-for-the-cloud-native-era/)
- kagent (Solo.io / CNCF Sandbox) — [Human-in-the-loop with kagent](https://kagent.dev/blog/human-in-the-loop-kagent), [Bringing agentic AI to Kubernetes](https://www.solo.io/blog/bringing-agentic-ai-to-kubernetes-contributing-kagent-to-cncf)
- PagerDuty — [Inside PagerDuty's SRE Agent](https://www.pagerduty.com/eng/inside-pagerdutys-sre-agent-how-we-built-deep-incident-investigation/) `[re-verification incomplete]`

**Research method note:** the deep-research run hit its session token limit before every claim finished
adversarial re-verification — 17 of 25 sampled claims completed independent 3-vote confirmation; the
remainder are flagged inline above.
