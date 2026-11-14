# Example Payloads & End-to-End Flow

## Scenario: `payment-api` in namespace `stage` has a CrashLoopBackOff

---

### 1. Claude Prompt (condensed example)

```
You are a senior Kubernetes / Site Reliability Engineer.
Analyze the following context from an unhealthy Kubernetes service in the stage environment...

Service name   : payment-api
Namespace      : stage
Issues         : Only 0/2 replicas are ready; Container payment-api: CrashLoopBackOff; Container payment-api has 8 restarts

POD STATUSES:
[
  {
    "name": "payment-api-7d6b9f-xk2lp",
    "phase": "Running",
    "containers": [{"name": "payment-api", "ready": false, "restart_count": 8, "reason": "CrashLoopBackOff"}],
    "healthy": false,
    "issues": ["Container payment-api: CrashLoopBackOff", "Container payment-api has 8 restarts"]
  }
]

KUBERNETES EVENTS:
[
  {"type": "Warning", "reason": "BackOff", "message": "Back-off restarting failed container payment-api in pod payment-api-7d6b9f-xk2lp_stage",
   "count": 42, "involved_object": "payment-api-7d6b9f-xk2lp"}
]

POD LOGS (last 100 lines):
--- payment-api-7d6b9f-xk2lp ---
2025-08-01T10:22:01Z INFO  Starting payment-api v2.3.1
2025-08-01T10:22:01Z ERROR Cannot connect to database: dial tcp 10.0.1.5:5432: connect: connection refused
2025-08-01T10:22:01Z FATAL Exiting due to missing required dependency
```

---

### 2. Claude JSON Response

```json
{
  "root_causes": [
    {
      "hypothesis": "Database connection failure at startup causing immediate crash",
      "confidence": "high",
      "evidence": "Log line: 'Cannot connect to database: dial tcp 10.0.1.5:5432: connect: connection refused'. Pod exits immediately after this, triggering CrashLoopBackOff."
    },
    {
      "hypothesis": "Database service at 10.0.1.5:5432 is down or unreachable from stage namespace",
      "confidence": "medium",
      "evidence": "The IP 10.0.1.5:5432 is a direct pod IP rather than a Kubernetes Service DNS name, which suggests possible misconfiguration or the target pod was rescheduled."
    }
  ],
  "remediation_steps": [
    {
      "priority": 1,
      "description": "Verify the database pod/service is running and reachable from the stage namespace",
      "kubectl_command": "kubectl get svc,pods -n stage -l app=postgres"
    },
    {
      "priority": 2,
      "description": "Check if payment-api is using a hardcoded IP instead of a Service DNS name. Update the DB_HOST env var to use the Kubernetes Service DNS (e.g., postgres.stage.svc.cluster.local)",
      "kubectl_command": "kubectl get deployment payment-api -n stage -o jsonpath='{.spec.template.spec.containers[0].env}'"
    },
    {
      "priority": 3,
      "description": "Temporarily add an initContainer that waits for the DB to be reachable before starting payment-api",
      "kubectl_command": null
    }
  ],
  "additional_checks": [
    "Run: kubectl exec -it <any-pod-in-stage> -- nc -zv 10.0.1.5 5432 to test reachability",
    "Check if a recent deployment changed the DB_HOST environment variable",
    "Inspect the postgres StatefulSet/Deployment for restarts or OOMKills"
  ],
  "severity": "critical",
  "summary": "payment-api is crash-looping because it cannot connect to its database at 10.0.1.5:5432, likely due to a hardcoded IP that is now stale or the database pod being down."
}
```

---

### 3. Teams Adaptive Card (raw JSON sent to webhook)

```json
{
  "type": "message",
  "attachments": [
    {
      "contentType": "application/vnd.microsoft.card.adaptive",
      "content": {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "type": "AdaptiveCard",
        "version": "1.4",
        "body": [
          {
            "type": "TextBlock",
            "text": "[STAGE][UNHEALTHY] payment-api",
            "size": "Large",
            "weight": "Bolder",
            "color": "Attention"
          },
          {
            "type": "FactSet",
            "facts": [
              { "title": "Namespace", "value": "stage" },
              { "title": "Severity", "value": "CRITICAL" },
              { "title": "Ready/Desired", "value": "0/2" },
              { "title": "Issues", "value": "Container payment-api: CrashLoopBackOff; 8 restarts" },
              { "title": "Detected at", "value": "2025-08-01 10:22 UTC" }
            ]
          },
          {
            "type": "TextBlock",
            "text": "**Summary:** payment-api crash-loops due to database connection failure at startup.",
            "wrap": true
          },
          {
            "type": "TextBlock",
            "text": "🔍 Root Cause Hypotheses",
            "weight": "Bolder"
          },
          {
            "type": "TextBlock",
            "text": "**[HIGH]** Database connection failure at startup causing immediate crash",
            "wrap": true
          },
          {
            "type": "TextBlock",
            "text": "_Evidence: Log: 'Cannot connect to database: dial tcp 10.0.1.5:5432: connection refused'_",
            "isSubtle": true,
            "wrap": true
          },
          {
            "type": "TextBlock",
            "text": "🔧 Remediation Steps",
            "weight": "Bolder"
          },
          {
            "type": "TextBlock",
            "text": "**1.** Verify the database pod/service is running",
            "wrap": true
          },
          {
            "type": "TextBlock",
            "text": "```\nkubectl get svc,pods -n stage -l app=postgres\n```",
            "fontType": "Monospace"
          },
          {
            "type": "TextBlock",
            "text": "⚡ Quick Commands",
            "weight": "Bolder"
          },
          {
            "type": "TextBlock",
            "text": "```\nkubectl get pods -n stage -l environment=stage\nkubectl describe deployment payment-api -n stage\nkubectl get events -n stage --sort-by='.lastTimestamp'\n```",
            "fontType": "Monospace"
          }
        ]
      }
    }
  ]
}
```

---

### 4. End-to-End Timeline

```
T+0s    CronJob fires (or 5-min interval elapses)
T+1s    discover_stage_services() — finds payment-api, order-api, user-api
T+2s    check_service_health(payment-api) — 0/2 ready, CrashLoopBackOff detected
T+3s    collect_context(payment-api) — fetches events, last 100 log lines, deployment spec
T+4s    analyze_with_claude(context) — sends ~3KB prompt, receives JSON analysis
T+6s    send_to_teams(alert) — POST Adaptive Card to webhook
T+6s    Teams channel receives formatted alert card
T+7s    check_service_health(order-api) — healthy ✓ skip
T+8s    check_service_health(user-api) — healthy ✓ skip
T+8s    Cycle complete, 1 alert sent
```
