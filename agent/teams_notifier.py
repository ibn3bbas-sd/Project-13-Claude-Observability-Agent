# Copyright 2026 Ibn Abbas
# SPDX-License-Identifier: Apache-2.0
"""
Formats and sends alert cards to a Microsoft Teams channel via Microsoft Graph API.
Uses OAuth2 delegated flow (refresh_token grant) and Adaptive Card format.
Supports polling for replies and posting follow-up responses in-thread.
"""

import base64
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

import requests

try:
    from .config import Config
except ImportError:
    from config import Config

logger = logging.getLogger(__name__)

MS_TOKEN_URL_TEMPLATE = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
MS_GRAPH_URL = "https://graph.microsoft.com/v1.0"

SEVERITY_COLORS = {
    "critical": "Attention",
    "high": "Warning",
    "medium": "Accent",
    "low": "Good",
    "unknown": "Default",
}


class TeamsNotifier:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._access_token: Optional[str] = None
        self._token_expiry: float = 0.0
        self._bot_user_id: Optional[str] = None

    def _get_token(self) -> str:
        # Refresh 60 s before expiry so we never use a token that is about to expire
        if self._access_token and time.monotonic() < self._token_expiry - 60:
            return self._access_token
        url = MS_TOKEN_URL_TEMPLATE.format(tenant=self.cfg.ms_graph_tenant_id)
        resp = requests.post(url, data={
            "client_id": self.cfg.ms_graph_client_id,
            "grant_type": "refresh_token",
            "refresh_token": self.cfg.ms_graph_refresh_token,
            "scope": "offline_access ChannelMessage.Send Channel.ReadBasic.All User.Read",
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expiry = time.monotonic() + int(data.get("expires_in", 3600))
        new_rt = data.get("refresh_token")
        if new_rt and new_rt != self.cfg.ms_graph_refresh_token:
            self.cfg.ms_graph_refresh_token = new_rt
            self._persist_refresh_token(new_rt)
        return self._access_token

    def _persist_refresh_token(self, new_token: str) -> None:
        """Write the rotated refresh token back to the in-cluster K8s secret."""
        sa_token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
        ns_path = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
        if not os.path.exists(sa_token_path):
            logger.warning("Not running in-cluster — cannot persist rotated refresh token")
            return
        try:
            with open(sa_token_path) as f:
                sa_token = f.read().strip()
            with open(ns_path) as f:
                namespace = f.read().strip()
            encoded = base64.b64encode(new_token.encode()).decode()
            resp = requests.patch(
                f"https://kubernetes.default.svc/api/v1/namespaces/{namespace}"
                "/secrets/claude-agent-secrets",
                headers={
                    "Authorization": f"Bearer {sa_token}",
                    "Content-Type": "application/strategic-merge-patch+json",
                },
                data=json.dumps({"data": {"MS_GRAPH_REFRESH_TOKEN": encoded}}),
                verify=ca_path,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("Rotated MS refresh token persisted to K8s secret claude-agent-secrets")
            else:
                logger.error(
                    "Failed to persist refresh token to K8s secret: HTTP %s %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            logger.warning("Could not persist refresh token to K8s secret: %s", exc)

    def _get_bot_user_id(self) -> Optional[str]:
        if self._bot_user_id:
            return self._bot_user_id
        try:
            token = self._get_token()
            resp = requests.get(
                f"{MS_GRAPH_URL}/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if resp.status_code == 200:
                self._bot_user_id = resp.json().get("id")
            return self._bot_user_id
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Send alert via Incoming Webhook (no auth, no membership check)
    # Returns None on success (no message ID available from webhooks).
    # ------------------------------------------------------------------
    def _send_via_webhook(self, card: Dict[str, Any]) -> None:
        payload = {
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card,
            }],
        }
        for attempt in range(1, 4):
            resp = requests.post(
                self.cfg.ms_teams_webhook_url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(payload),
                timeout=30,
            )
            if resp.status_code == 200:
                return
            if resp.status_code >= 500 and attempt < 3:
                wait = 2 ** attempt
                logger.warning(
                    "Webhook HTTP %s (attempt %d/3) — retrying in %ds",
                    resp.status_code, attempt, wait,
                )
                time.sleep(wait)
                continue
            logger.error(
                "Webhook send failed: HTTP %s %s",
                resp.status_code, resp.text[:300],
            )
            resp.raise_for_status()

    # ------------------------------------------------------------------
    # Send alert (returns message ID for threading, or None for webhook)
    # ------------------------------------------------------------------
    def send_to_teams(
        self,
        svc: Dict[str, Any],
        health_report: Dict[str, Any],
        analysis: Dict[str, Any],
        gitops_pr_url: Optional[str] = None,
    ) -> Optional[str]:
        card = self._build_card(svc, health_report, analysis, gitops_pr_url=gitops_pr_url)

        if self.cfg.ms_teams_webhook_url:
            logger.info(
                "Sending Teams alert for %s/%s via Incoming Webhook",
                svc["namespace"], svc["name"],
            )
            self._send_via_webhook(card)
            logger.info("Teams alert sent successfully via Incoming Webhook")
            return None  # no message ID — thread replies not available

        token = self._get_token()
        attachment_id = str(uuid.uuid4())
        body = {
            "body": {
                "contentType": "html",
                "content": f'<attachment id="{attachment_id}"></attachment>',
            },
            "attachments": [{
                "id": attachment_id,
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": json.dumps(card),
            }],
        }

        logger.info("Sending Teams alert for %s/%s", svc["namespace"], svc["name"])
        url = (
            f"{MS_GRAPH_URL}/teams/{self.cfg.ms_graph_team_id}"
            f"/channels/{self.cfg.ms_graph_channel_id}/messages"
        )
        for attempt in range(1, 4):
            resp = requests.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                data=json.dumps(body),
                timeout=30,
            )
            if resp.status_code in (200, 201):
                msg_id = resp.json().get("id")
                logger.info("Teams alert sent successfully (msg id %s)", msg_id)
                return msg_id
            if resp.status_code >= 500 and attempt < 3:
                wait = 2 ** attempt
                logger.warning(
                    "Teams alert HTTP %s (attempt %d/3) — retrying in %ds",
                    resp.status_code, attempt, wait,
                )
                time.sleep(wait)
                token = self._get_token()
                continue
            if resp.status_code == 403:
                error_body = resp.text[:500]
                if "AclCheckFailed" in error_body or "not a member" in error_body.lower():
                    logger.error(
                        "Teams 403 — service account is NOT a member of the channel. "
                        "Re-add via Graph API:\n"
                        "  POST https://graph.microsoft.com/v1.0/teams/%s/members\n"
                        "  Body: {\"@odata.type\":\"#microsoft.graph.aadUserConversationMember\","
                        "\"roles\":[\"member\"],"
                        "\"user@odata.bind\":\"https://graph.microsoft.com/v1.0/users/<object_id>\"}\n"
                        "Full error: %s",
                        self.cfg.ms_graph_team_id, error_body,
                    )
                else:
                    logger.error(
                        "Teams 403 Forbidden (token scope or consent issue): %s", error_body,
                    )
                resp.raise_for_status()
            logger.error(
                "Teams alert failed: HTTP %s %s",
                resp.status_code,
                resp.text[:300],
            )
            resp.raise_for_status()
        return None

    # ------------------------------------------------------------------
    # Poll for relevant messages (thread replies + @mentions in channel)
    # ------------------------------------------------------------------
    def poll_for_replies(
        self,
        message_id: str,
        on_reply: Callable[[Dict[str, Any]], None],
    ) -> None:
        timeout = self.cfg.teams_reply_timeout
        interval = self.cfg.teams_poll_interval

        if timeout <= 0:
            return

        bot_user_id = self._get_bot_user_id()
        logger.info(
            "Listening for thread replies and @mentions (timeout=%ds, interval=%ds, bot_user=%s)",
            timeout, interval, bot_user_id,
        )

        seen_ids: set = set()
        deadline = time.monotonic() + timeout
        token = self._get_token()
        base = (
            f"{MS_GRAPH_URL}/teams/{self.cfg.ms_graph_team_id}"
            f"/channels/{self.cfg.ms_graph_channel_id}"
        )
        thread_replies_url = f"{base}/messages/{message_id}/replies"
        channel_messages_url = f"{base}/messages?$top=20&$orderby=createdDateTime+desc"

        while time.monotonic() < deadline:
            time.sleep(interval)
            try:
                headers = {"Authorization": f"Bearer {token}"}

                # 1. Thread replies — always relevant
                if not self._fetch_and_process(
                    thread_replies_url, headers, seen_ids, bot_user_id,
                    on_reply, require_mention=False,
                ):
                    logger.warning(
                        "Reply listener requires ChannelMessage.Read.All permission "
                        "(add it in Azure AD app registration and grant admin consent)"
                    )
                    return

                # 2. Channel messages — only if delegate user is @mentioned
                # Failure here is non-fatal (requires ChannelMessage.Read.All)
                self._fetch_and_process(
                    channel_messages_url, headers, seen_ids, bot_user_id,
                    on_reply, require_mention=True, silent_4xx=True,
                )

            except requests.RequestException as exc:
                logger.warning("Error polling for messages: %s", exc)

        logger.info("Polling complete for message %s", message_id)

    def _fetch_and_process(
        self,
        url: str,
        headers: Dict[str, str],
        seen_ids: set,
        bot_user_id: Optional[str],
        on_reply: Callable[[Dict[str, Any]], None],
        require_mention: bool,
        silent_4xx: bool = False,
    ) -> bool:
        """Returns False on 4xx (permission denied), True otherwise."""
        resp = requests.get(url, headers=headers, timeout=15)
        if 400 <= resp.status_code < 500:
            if not silent_4xx:
                logger.warning(
                    "Reply listener got HTTP %s from %s "
                    "(missing ChannelMessage.Read.All permission — add it in Azure AD)",
                    resp.status_code, url[:80],
                )
            return False
        if resp.status_code != 200:
            logger.warning("Failed to fetch messages from %s: HTTP %s", url[:80], resp.status_code)
            return True

        for msg in resp.json().get("value", []):
            msg_id = msg.get("id")
            if not msg_id or msg_id in seen_ids:
                continue

            sender = msg.get("from", {}).get("user", {})
            sender_id = sender.get("id")
            if bot_user_id and sender_id == bot_user_id:
                seen_ids.add(msg_id)
                continue

            if require_mention:
                if not self._mentions_bot(msg, bot_user_id):
                    continue

            seen_ids.add(msg_id)

            body_content = msg.get("body", {}).get("content", "")
            author = sender.get("displayName", "Unknown")
            created = msg.get("createdDateTime", "")
            source = "channel @mention" if require_mention else "thread reply"

            logger.info(
                "Accepted %s from %s at %s: %s",
                source, author, created, body_content[:200],
            )
            on_reply({
                "author": author,
                "text": body_content,
                "timestamp": created,
                "reply_id": msg_id,
            })
        return True

    @staticmethod
    def _mentions_bot(msg: Dict[str, Any], bot_user_id: Optional[str]) -> bool:
        if not bot_user_id:
            return False
        for mention in msg.get("mentions", []):
            mentioned_user = mention.get("mentioned", {}).get("user", {})
            if mentioned_user.get("id") == bot_user_id:
                return True
        return False

    # ------------------------------------------------------------------
    # Internal: POST an Adaptive Card reply to a thread with 5xx retry
    # ------------------------------------------------------------------
    def _post_to_thread(self, message_id: str, card: Dict[str, Any]) -> Optional[str]:
        token = self._get_token()
        attachment_id = str(uuid.uuid4())
        body = {
            "body": {
                "contentType": "html",
                "content": f'<attachment id="{attachment_id}"></attachment>',
            },
            "attachments": [{
                "id": attachment_id,
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": json.dumps(card),
            }],
        }
        url = (
            f"{MS_GRAPH_URL}/teams/{self.cfg.ms_graph_team_id}"
            f"/channels/{self.cfg.ms_graph_channel_id}"
            f"/messages/{message_id}/replies"
        )
        for attempt in range(1, 4):
            resp = requests.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                data=json.dumps(body),
                timeout=30,
            )
            if resp.status_code in (200, 201):
                return resp.json().get("id")
            if resp.status_code >= 500 and attempt < 3:
                wait = 2 ** attempt
                logger.warning(
                    "Thread reply HTTP %s (attempt %d/3) — retrying in %ds",
                    resp.status_code, attempt, wait,
                )
                time.sleep(wait)
                token = self._get_token()
                continue
            logger.error(
                "Failed to post thread reply: HTTP %s %s",
                resp.status_code, resp.text[:300],
            )
            return None
        return None

    # ------------------------------------------------------------------
    # Post a structured follow-up card to an existing thread
    # ------------------------------------------------------------------
    def post_followup_card(self, message_id: str, followup: Dict[str, Any]) -> Optional[str]:
        """Post a compact Adaptive Card reply from a structured follow-up dict."""
        body_items: List[Dict] = [
            {
                "type": "TextBlock",
                "text": followup.get("diagnosis", ""),
                "wrap": True,
                "weight": "Bolder",
            },
        ]

        for i, step in enumerate(followup.get("steps", []), start=1):
            desc = step.get("description", "")
            cmd = step.get("command")
            body_items.append({
                "type": "TextBlock",
                "text": f"**{i}.** {desc}",
                "wrap": True,
                "spacing": "Small",
            })
            if cmd:
                body_items.append({
                    "type": "TextBlock",
                    "text": f"`{cmd}`",
                    "fontType": "Monospace",
                    "wrap": True,
                    "spacing": "None",
                    "isSubtle": True,
                })

        note = followup.get("note")
        if note:
            body_items.append({
                "type": "TextBlock",
                "text": f"⚠️ {note}",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Medium",
            })

        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": body_items,
        }
        reply_id = self._post_to_thread(message_id, card)
        if reply_id:
            logger.info("Posted follow-up card to thread %s (reply id %s)", message_id, reply_id)
        return reply_id

    # ------------------------------------------------------------------
    # Post a command-execution result card to an existing thread
    # ------------------------------------------------------------------
    def post_command_card(
        self,
        message_id: str,
        command: str,
        output: str,
        success: bool,
    ) -> Optional[str]:
        icon = "✅" if success else "❌"
        color = "Good" if success else "Attention"
        # Ensure output is a string (k8s client may return bytes)
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": f"{icon} Command executed",
                    "weight": "Bolder",
                    "color": color,
                },
                {
                    "type": "TextBlock",
                    "text": f"`{command}`",
                    "fontType": "Monospace",
                    "wrap": True,
                    "spacing": "Small",
                },
                {
                    "type": "TextBlock",
                    "text": str(output)[:400],
                    "wrap": True,
                    "isSubtle": True,
                    "spacing": "Small",
                },
            ],
        }
        return self._post_to_thread(message_id, card)

    def post_status_card(
        self,
        message_id: str,
        svc_name: str,
        still_unhealthy: bool,
        summary: str,
    ) -> Optional[str]:
        if still_unhealthy:
            icon, color, title = "⏳", "Warning", f"Still unhealthy — {svc_name}"
        else:
            icon, color, title = "✅", "Good", f"Resolved — {svc_name}"
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": f"{icon} {title}",
                    "weight": "Bolder",
                    "color": color,
                },
                {
                    "type": "TextBlock",
                    "text": summary,
                    "wrap": True,
                    "isSubtle": True,
                    "spacing": "Small",
                },
            ],
        }
        return self._post_to_thread(message_id, card)

    # ------------------------------------------------------------------
    # Post a text reply to an existing thread
    # ------------------------------------------------------------------
    def post_reply(self, message_id: str, text: str) -> Optional[str]:
        token = self._get_token()
        body = {
            "body": {
                "contentType": "text",
                "content": text,
            },
        }
        resp = requests.post(
            f"{MS_GRAPH_URL}/teams/{self.cfg.ms_graph_team_id}"
            f"/channels/{self.cfg.ms_graph_channel_id}"
            f"/messages/{message_id}/replies",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            data=json.dumps(body),
            timeout=30,
        )
        if resp.status_code in (200, 201):
            reply_id = resp.json().get("id")
            logger.info("Posted reply to thread %s (reply id %s)", message_id, reply_id)
            return reply_id
        else:
            logger.error(
                "Failed to post reply: HTTP %s %s",
                resp.status_code, resp.text[:300],
            )
            return None

    # ------------------------------------------------------------------
    # Post a resolution card as a reply to an existing alert thread
    # ------------------------------------------------------------------
    def send_resolution_reply(
        self,
        message_id: str,
        svc_name: str,
        command: str,
        output: str,
    ) -> Optional[str]:
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        card = {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [
                {
                    "type": "TextBlock",
                    "text": f"[RESOLVED] {svc_name}",
                    "size": "Medium",
                    "weight": "Bolder",
                    "color": "Good",
                },
                {
                    "type": "TextBlock",
                    "text": f"**Command:** `{command}`",
                    "wrap": True,
                    "spacing": "Small",
                },
                {
                    "type": "TextBlock",
                    "text": str(output)[:400],
                    "wrap": True,
                    "fontType": "Monospace",
                    "isSubtle": True,
                    "spacing": "None",
                },
            ],
        }
        reply_id = self._post_to_thread(message_id, card)
        if reply_id:
            logger.info("Posted resolution card to thread %s (reply id %s)", message_id, reply_id)
        return reply_id

    # ------------------------------------------------------------------
    # Build Adaptive Card (compact: title + facts + summary + top cause + top fix)
    # ------------------------------------------------------------------
    def _build_card(
        self,
        svc: Dict[str, Any],
        health_report: Dict[str, Any],
        analysis: Dict[str, Any],
        gitops_pr_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        ns = svc["namespace"]
        name = svc["name"]
        severity = analysis.get("severity", "unknown").lower()
        color = SEVERITY_COLORS.get(severity, "Default")
        from datetime import timedelta
        local_tz = timezone.utc
        timestamp = datetime.now(local_tz).strftime("%Y-%m-%d %H:%M UTC")
        summary_text = analysis.get("summary", health_report.get("summary", "Service unhealthy"))

        card_body: List[Dict] = [
            {
                "type": "TextBlock",
                "text": "🌐 [STAGE]",
                "size": "Small",
                "weight": "Bolder",
                "isSubtle": True,
                "spacing": "None",
            },
            {
                "type": "TextBlock",
                "text": f"🚨 [UNHEALTHY] {name}",
                "size": "Large",
                "weight": "Bolder",
                "color": color,
                "wrap": True,
                "spacing": "None",
            },
            {
                "type": "FactSet",
                "facts": [
                    {"title": "📁 Namespace", "value": ns},
                    {"title": "🗂️ Resource", "value": svc.get("resource_type", "Service")},
                    {"title": "⚠️ Severity", "value": severity.upper()},
                    {"title": "🔄 Ready/Desired", "value": f"{health_report['ready_replicas']}/{health_report['desired_replicas']}"},
                    {"title": "🕒 Detected", "value": timestamp},
                ],
                "spacing": "Small",
            },
            {
                "type": "TextBlock",
                "text": f"📋 {summary_text}",
                "wrap": True,
                "spacing": "Medium",
            },
        ]

        # When the analysis came back unusable (unparseable JSON, API error), the card
        # would otherwise carry only the failure sentence — no cause, no command, nothing
        # to act on. Fall back to the health checker's own findings, which are always
        # available, and show what Claude actually said so it can be debugged from Teams.
        if not analysis.get("root_causes") and not analysis.get("remediation_steps"):
            issues = health_report.get("issues", [])
            if issues:
                card_body.append({
                    "type": "TextBlock",
                    "text": "🔍 **Detected issues:**\n"
                            + "\n".join(f"- {i}" for i in issues[:5]),
                    "wrap": True,
                    "spacing": "Small",
                })
            raw = analysis.get("_raw")
            if raw:
                card_body.append({
                    "type": "TextBlock",
                    "text": f"🧩 **Unparsed model output:** {raw[:600]}",
                    "wrap": True,
                    "isSubtle": True,
                    "fontType": "Monospace",
                    "size": "Small",
                    "spacing": "Small",
                })

        # Top root cause only
        root_causes = analysis.get("root_causes", [])
        if root_causes:
            rc = root_causes[0]
            card_body.append({
                "type": "TextBlock",
                "text": f"🔍 **Cause [{rc.get('confidence', '?').upper()}]:** {rc.get('hypothesis', '')}",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small",
            })

        # Top remediation step + first available kubectl command across all steps
        steps = sorted(
            analysis.get("remediation_steps", []), key=lambda x: x.get("priority", 99)
        )
        if steps:
            step = steps[0]
            card_body.append({
                "type": "TextBlock",
                "text": f"🔧 **Fix:** {step.get('description', '')}",
                "wrap": True,
                "spacing": "Medium",
            })
            if gitops_pr_url:
                # A live kubectl patch here would just be reverted by the next Helm/GitOps
                # reconcile — show the durable fix instead of the doomed live-patch command.
                card_body.append({
                    "type": "TextBlock",
                    "text": f"🔀 **GitOps fix opened:** [{gitops_pr_url}]({gitops_pr_url}) "
                            "(a live kubectl patch here would be reverted by Helm)",
                    "wrap": True,
                    "spacing": "None",
                })
            else:
                # Show the kubectl command from the top step, or fall back to the first step
                # that has one (in case priority-1 step is non-actionable, e.g. a connectivity issue).
                cmd = step.get("kubectl_command") or next(
                    (s["kubectl_command"] for s in steps[1:] if s.get("kubectl_command")), None
                )
                if cmd:
                    card_body.append({
                        "type": "TextBlock",
                        "text": f"`{cmd}`",
                        "fontType": "Monospace",
                        "wrap": True,
                        "spacing": "None",
                    })

        return {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": card_body,
        }
