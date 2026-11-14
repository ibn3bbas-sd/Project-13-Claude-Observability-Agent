"""
Configuration — loaded entirely from environment variables.
No secrets are ever logged.
"""

import os
import logging
from typing import List

logger = logging.getLogger(__name__)


class Config:
    # Kubernetes
    namespaces: List[str]
    kubeconfig_path: str
    use_in_cluster: bool
    stage_label_selector: str

    # Schedule
    interval_seconds: int
    run_once: bool

    # Claude
    anthropic_api_key: str
    claude_model: str
    claude_max_tokens: int

    # Microsoft Graph (Teams)
    ms_graph_tenant_id: str
    ms_graph_client_id: str
    ms_graph_refresh_token: str
    ms_graph_team_id: str
    ms_graph_channel_id: str

    # Teams reply polling
    teams_reply_timeout: int
    teams_poll_interval: int

    # Limits to avoid huge payloads
    max_log_lines: int
    max_events: int

    def __init__(self) -> None:
        # Kubernetes
        ns_raw = os.environ.get("NAMESPACES", "stage")
        self.namespaces = [n.strip() for n in ns_raw.split(",") if n.strip()]
        self.kubeconfig_path = os.environ.get("KUBECONFIG", "")
        self.use_in_cluster = os.environ.get("USE_IN_CLUSTER", "false").lower() == "true"
        self.stage_label_selector = os.environ.get("STAGE_LABEL_SELECTOR", "environment=stage")

        # Schedule
        self.interval_seconds = int(os.environ.get("INTERVAL_SECONDS", "300"))
        self.run_once = os.environ.get("RUN_ONCE", "false").lower() == "true"
        self.test_alert = os.environ.get("TEST_ALERT", "false").lower() == "true"

        # Claude
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        # claude-sonnet-5 supports structured outputs (output_config.format), which
        # guarantees schema-valid JSON — claude-sonnet-4-6 does not. See
        # STRUCTURED_OUTPUT_MODEL_PREFIXES in claude_analyzer.py.
        self.claude_model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
        # Sonnet 5 runs adaptive thinking by default, and thinking shares this budget
        # with the response text — 4096 truncated the JSON mid-object.
        self.claude_max_tokens = int(os.environ.get("CLAUDE_MAX_TOKENS", "8192"))

        # Teams notifications — webhook URL (preferred) or Graph API delegated flow
        self.ms_teams_webhook_url = os.environ.get("MS_TEAMS_WEBHOOK_URL", "")

        # Microsoft Graph (Teams) — only required when MS_TEAMS_WEBHOOK_URL is not set
        self.ms_graph_tenant_id = os.environ.get("MS_GRAPH_TENANT_ID", "")
        self.ms_graph_client_id = os.environ.get("MS_GRAPH_CLIENT_ID", "")
        self.ms_graph_refresh_token = os.environ.get("MS_GRAPH_REFRESH_TOKEN", "")
        self.ms_graph_team_id = os.environ.get("MS_GRAPH_TEAM_ID", "")
        self.ms_graph_channel_id = os.environ.get("MS_GRAPH_CHANNEL_ID", "")

        # Teams reply polling
        self.teams_reply_timeout = int(os.environ.get("TEAMS_REPLY_TIMEOUT", "300"))
        self.teams_poll_interval = int(os.environ.get("TEAMS_POLL_INTERVAL", "10"))

        # Limits
        self.max_log_lines = int(os.environ.get("MAX_LOG_LINES", "100"))
        self.max_events = int(os.environ.get("MAX_EVENTS", "60"))

        # Namespace the agent itself runs in — used for its own ConfigMaps
        # (alert cooldown, remediation outcome store), not the monitored services.
        self.agent_namespace = os.environ.get("POD_NAMESPACE", "monitoring")

        # Observability integrations (optional — gracefully skipped if empty)
        self.prometheus_url = os.environ.get("PROMETHEUS_URL", "").rstrip("/")
        self.loki_url = os.environ.get("LOKI_URL", "").rstrip("/")
        self.loki_lookback_hours = int(os.environ.get("LOKI_LOOKBACK_HOURS", "1"))
        self.loki_max_lines = int(os.environ.get("LOKI_MAX_LINES", "200"))
        self.tempo_url = os.environ.get("TEMPO_URL", "").rstrip("/")

        # GitOps-first remediation for Helm-owned drift (opt-in per ingress — see
        # agent/gitops_remediation.py). Empty map means this is a complete no-op.
        self.github_token = os.environ.get("GITHUB_TOKEN", "")
        self.gitops_repo_map_json = os.environ.get("GITOPS_REPO_MAP", "")

    def validate(self) -> None:
        errors = []
        if not self.anthropic_api_key:
            errors.append("ANTHROPIC_API_KEY is required")
        if not self.ms_teams_webhook_url:
            # Webhook not configured — require full Graph API credentials
            for var in ("ms_graph_tenant_id", "ms_graph_client_id",
                        "ms_graph_refresh_token", "ms_graph_team_id",
                        "ms_graph_channel_id"):
                if not getattr(self, var):
                    errors.append(f"{var.upper()} is required (or set MS_TEAMS_WEBHOOK_URL)")
        if not self.namespaces:
            errors.append("NAMESPACES must contain at least one value")
        if errors:
            for e in errors:
                logger.error("Config error: %s", e)
            raise ValueError("Invalid configuration: " + "; ".join(errors))
        mode = "webhook" if self.ms_teams_webhook_url else "Graph API (delegated)"
        logger.info(
            "Config loaded — namespaces=%s model=%s interval=%ds teams_mode=%s",
            self.namespaces,
            self.claude_model,
            self.interval_seconds,
            mode,
        )
