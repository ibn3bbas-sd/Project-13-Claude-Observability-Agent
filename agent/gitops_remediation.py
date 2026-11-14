# Copyright (c) 2026 Ibn Abbas
# SPDX-License-Identifier: MIT
"""
GitOps-first remediation for Helm-owned drift (roadmap P2 item 8).

The documented recurring problem (see agent/monitor.py's ingress cooldown comment
and CLAUDE.md): a live `kubectl patch ingress` fix for a duplicate-hostname
conflict gets reverted on the next Helm/GitOps reconcile, so the same alert
fires again every cooldown window forever. HolmesGPT's "Operator Mode" pattern
is the correct fix — open a PR against the owning chart's values file instead
of patching the live object, so the fix survives reconciliation.

Opt-in per ingress via GITOPS_REPO_MAP (a JSON env var mapping
"namespace/name" -> {"repo": "owner/repo", "path": "values.yaml",
"yaml_path": "ingress.hosts[0].host", "base_branch": "main"}). With no entry
for a given ingress, this module does nothing and behavior is unchanged from
before this feature existed — there is no way to guess which repo/chart owns
an arbitrary monitored service's ingress, so this is deliberately opt-in
rather than something this agent tries to infer.
"""

import base64
import json
import logging
import re
from typing import Any, Dict, Optional, Tuple

import requests
import yaml

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 15
_GITHUB_API = "https://api.github.com"

_HOSTNAME_PATCH_RE = re.compile(r'"path"\s*:\s*"/spec/rules/\d+/host"\s*,\s*"value"\s*:\s*"([^"]+)"')
_PATH_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def load_repo_map(raw_json: str) -> Dict[str, Dict[str, Any]]:
    if not raw_json:
        return {}
    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("GITOPS_REPO_MAP is not valid JSON — GitOps remediation disabled")
        return {}


def lookup_mapping(
    repo_map: Dict[str, Dict[str, Any]], namespace: str, name: str
) -> Optional[Dict[str, Any]]:
    return repo_map.get(f"{namespace}/{name}")


def extract_target_hostname(analysis: Dict[str, Any]) -> Optional[str]:
    """Pull the new hostname out of a suggested 'kubectl patch ingress ... host' command."""
    for step in analysis.get("remediation_steps", []) or []:
        cmd = step.get("kubectl_command") or ""
        if "patch" not in cmd or "ingress" not in cmd:
            continue
        match = _HOSTNAME_PATCH_RE.search(cmd)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Tiny YAML path navigation, e.g. "ingress.hosts[0].host"
# ---------------------------------------------------------------------------
def _walk_path(path: str):
    for key, idx in _PATH_SEGMENT_RE.findall(path):
        yield ("key", key) if key else ("index", int(idx))


def set_nested(data: Any, path: str, value: Any) -> None:
    tokens = list(_walk_path(path))
    if not tokens:
        raise ValueError(f"Empty yaml_path: {path!r}")
    node = data
    for _kind, token in tokens[:-1]:
        node = node[token]
    _last_kind, last_token = tokens[-1]
    node[last_token] = value


# ---------------------------------------------------------------------------
# GitHub REST calls
# ---------------------------------------------------------------------------
def _gh(method: str, url: str, token: str, **kwargs) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    return requests.request(method, url, headers=headers, timeout=_HTTP_TIMEOUT, **kwargs)


def open_hostname_pr(
    token: str,
    mapping: Dict[str, Any],
    namespace: str,
    ingress_name: str,
    new_hostname: str,
    incident_summary: str,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Open (or find an already-open) PR that sets mapping['yaml_path'] to
    new_hostname in mapping['path'] on mapping['repo']. Returns (pr_url, error).
    Idempotent — safe to call again on every cooldown cycle while the
    conflict persists; it won't open a second PR for the same branch.
    """
    if not token:
        return None, "GitOps remediation requires GITHUB_TOKEN"
    repo = mapping.get("repo")
    path = mapping.get("path")
    yaml_path = mapping.get("yaml_path")
    base_branch = mapping.get("base_branch", "main")
    if not repo or not path or not yaml_path:
        return None, f"Incomplete GitOps mapping for {namespace}/{ingress_name} — need repo, path, yaml_path"

    owner = repo.split("/", 1)[0]
    branch = f"claude-agent/fix-ingress-hostname-{namespace}-{ingress_name}"

    try:
        existing_url = _find_open_pr(repo, owner, branch, token)
        if existing_url:
            return existing_url, None

        file_resp = _gh("GET", f"{_GITHUB_API}/repos/{repo}/contents/{path}", token,
                         params={"ref": base_branch})
        if file_resp.status_code != 200:
            return None, f"Could not read {path} from {repo}@{base_branch}: HTTP {file_resp.status_code}"
        file_json = file_resp.json()
        content = base64.b64decode(file_json["content"]).decode("utf-8")
        values = yaml.safe_load(content)
        set_nested(values, yaml_path, new_hostname)
        new_content = yaml.safe_dump(values, sort_keys=False)

        ref_resp = _gh("GET", f"{_GITHUB_API}/repos/{repo}/git/ref/heads/{base_branch}", token)
        if ref_resp.status_code != 200:
            return None, f"Could not read base branch ref for {repo}: HTTP {ref_resp.status_code}"
        base_sha = ref_resp.json()["object"]["sha"]

        branch_resp = _gh("POST", f"{_GITHUB_API}/repos/{repo}/git/refs", token,
                           json={"ref": f"refs/heads/{branch}", "sha": base_sha})
        if branch_resp.status_code not in (201, 422):  # 422 = branch already exists, fine
            return None, f"Could not create branch {branch}: HTTP {branch_resp.status_code}"

        update_resp = _gh("PUT", f"{_GITHUB_API}/repos/{repo}/contents/{path}", token, json={
            "message": f"fix: correct ingress hostname for {namespace}/{ingress_name}",
            "content": base64.b64encode(new_content.encode("utf-8")).decode("ascii"),
            "sha": file_json["sha"],
            "branch": branch,
        })
        if update_resp.status_code not in (200, 201):
            return None, (
                f"Could not update {path} on {branch}: "
                f"HTTP {update_resp.status_code}: {update_resp.text[:300]}"
            )

        pr_resp = _gh("POST", f"{_GITHUB_API}/repos/{repo}/pulls", token, json={
            "title": f"fix(ingress): correct hostname for {namespace}/{ingress_name}",
            "head": branch,
            "base": base_branch,
            "body": (
                f"Opened automatically by claude-agent after detecting a duplicate-hostname "
                f"conflict on ingress `{namespace}/{ingress_name}`.\n\n"
                f"A live `kubectl patch` would be reverted by the next Helm/GitOps reconcile — "
                f"this PR fixes it at the source instead.\n\n"
                f"**Incident summary:** {incident_summary}\n\n"
                f"New hostname: `{new_hostname}`"
            ),
        })
        if pr_resp.status_code == 201:
            return pr_resp.json()["html_url"], None
        if pr_resp.status_code == 422:
            existing_url = _find_open_pr(repo, owner, branch, token)
            if existing_url:
                return existing_url, None
        return None, f"Could not open PR: HTTP {pr_resp.status_code}: {pr_resp.text[:300]}"

    except Exception as exc:  # pylint: disable=broad-except
        return None, f"GitOps PR flow failed: {exc}"


def _find_open_pr(repo: str, owner: str, branch: str, token: str) -> Optional[str]:
    resp = _gh("GET", f"{_GITHUB_API}/repos/{repo}/pulls", token,
               params={"head": f"{owner}:{branch}", "state": "open"})
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["html_url"]
    return None
