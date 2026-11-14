"""
claude-agent
Monitors Kubernetes services in the stage environment, analyzes issues with Claude,
and sends alerts to Microsoft Teams.
"""

import os
import re
import sys
import json
import time
import shlex
import logging
import datetime as dt
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from kubernetes import client as k8s_client
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream as k8s_stream

try:
    from .health_checker import HealthChecker
    from .context_collector import ContextCollector
    from .claude_analyzer import ClaudeAnalyzer
    from .teams_notifier import TeamsNotifier
    from .config import Config
    from .outcome_store import save_outcome
    from . import gitops_remediation as gitops
except ImportError:
    from health_checker import HealthChecker
    from context_collector import ContextCollector
    from claude_analyzer import ClaudeAnalyzer
    from teams_notifier import TeamsNotifier
    from config import Config
    from outcome_store import save_outcome
    import gitops_remediation as gitops

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("claude-agent")


# ---------------------------------------------------------------------------
# kubectl command execution (via Python k8s client — no binary required)
# ---------------------------------------------------------------------------

_MUTATING_VERBS = {"rollout", "scale", "delete", "set", "patch", "apply", "create"}


def _extract_kubectl_command(html_text: str) -> Optional[str]:
    """Strip HTML tags and return the first kubectl command found, or None."""
    plain = re.sub(r"<[^>]+>", " ", html_text)
    plain = re.sub(r"&nbsp;", " ", plain).strip()
    match = re.search(r"(kubectl\s+\S.*)", plain, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _is_mutating(command: str) -> bool:
    parts = command.lower().split()
    return len(parts) >= 2 and parts[1] in _MUTATING_VERBS


# Blast-radius policy per remediation action, keyed by the same action names
# CommandExecutor._dispatch routes on. tier 1 = low blast-radius / reversible,
# runs as-is once a human has approved the command via Teams reply. tier 2 =
# mutates object spec directly — validated with a K8s server-side dry-run
# before the real call, so a malformed patch never touches the live object.
# Keep in sync with CommandExecutor._dispatch and classify_action() below.
ACTION_POLICY: Dict[str, Dict[str, Any]] = {
    "rollout_restart":             {"tier": 1, "dry_run": False},
    "delete_pod":                  {"tier": 1, "dry_run": False},
    "scale":                       {"tier": 1, "dry_run": False},
    "set_image":                   {"tier": 2, "dry_run": True},
    "patch_deployment":            {"tier": 2, "dry_run": True},
    "patch_ingress":               {"tier": 2, "dry_run": True},
    "patch_service":               {"tier": 2, "dry_run": True},
    "create_service_externalname": {"tier": 2, "dry_run": False},
}


def classify_action(command: str) -> Optional[str]:
    """Map a kubectl command string to an ACTION_POLICY key, or None if the
    command is read-only / unrecognized (get, exec, logs, rollout status)."""
    try:
        parts = shlex.split(command.strip())
    except ValueError:
        return None
    if len(parts) < 2 or parts[0].lower() != "kubectl":
        return None
    cmd = parts[1].lower()
    rest = parts[2:]
    sub = rest[0].lower() if rest else ""
    if cmd == "rollout" and sub == "restart":
        return "rollout_restart"
    if cmd == "scale":
        return "scale"
    if cmd == "delete":
        return "delete_pod"
    if cmd == "set" and sub == "image":
        return "set_image"
    if cmd == "create" and sub == "service":
        return "create_service_externalname"
    if cmd == "patch" and sub == "ingress":
        return "patch_ingress"
    if cmd == "patch" and sub in ("deployment", "deploy"):
        return "patch_deployment"
    if cmd == "patch" and sub in ("service", "svc"):
        return "patch_service"
    return None


def execute_with_policy(command: str, executor: "CommandExecutor") -> Tuple[bool, str]:
    """Run `command` through CommandExecutor honoring ACTION_POLICY.

    For tier-2 actions this first runs a K8s server-side dry-run; if that
    fails (bad patch JSON, RBAC denial, no such object, …) the real mutating
    call is never attempted. Everything else executes directly, unchanged.
    """
    action = classify_action(command)
    policy = ACTION_POLICY.get(action) if action else None
    if policy:
        logger.info("Action '%s' classified as policy tier %d (dry_run=%s)",
                     action, policy["tier"], policy["dry_run"])
    if not policy or not policy["dry_run"]:
        return executor.execute(command)

    ok, preview = executor.execute(command, dry_run=True)
    if not ok:
        return False, f"Dry-run validation failed, real command was not run: {preview}"
    return executor.execute(command)


class CommandExecutor:
    """Executes a limited subset of kubectl commands via the Python k8s client."""

    # kubernetes config was already loaded by HealthChecker.__init__
    def __init__(self) -> None:
        self.apps_v1 = k8s_client.AppsV1Api()
        self.core_v1 = k8s_client.CoreV1Api()
        self.networking_v1 = k8s_client.NetworkingV1Api()

    def execute(self, command: str, dry_run: bool = False) -> Tuple[bool, str]:
        parts = shlex.split(command.strip())
        if not parts or parts[0].lower() != "kubectl":
            return False, "Only kubectl commands are supported."
        try:
            return self._dispatch(parts[1:], dry_run=dry_run)
        except ApiException as exc:
            return False, f"Kubernetes API error {exc.status}: {exc.reason}"
        except Exception as exc:  # pylint: disable=broad-except
            return False, f"Error: {exc}"

    # ------------------------------------------------------------------
    def _dispatch(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        if not args:
            return False, "No subcommand given."
        cmd = args[0].lower()

        if cmd == "rollout" and len(args) >= 2:
            sub = args[1].lower()
            if sub == "restart":
                return self._rollout_restart(args[2:], dry_run=dry_run)
            if sub == "status":
                return self._rollout_status(args[2:])
            return False, f"rollout {sub} is not supported. Use: rollout restart|status deployment <name> -n <ns>"

        if cmd == "scale":
            return self._scale(args[1:], dry_run=dry_run)
        if cmd == "delete":
            return self._delete(args[1:], dry_run=dry_run)
        if cmd == "set" and len(args) >= 2 and args[1].lower() == "image":
            return self._set_image(args[2:], dry_run=dry_run)
        if cmd == "create" and len(args) >= 2 and args[1].lower() == "service":
            return self._create_service(args[2:], dry_run=dry_run)
        if cmd == "patch" and len(args) >= 2:
            sub = args[1].lower()
            if sub == "ingress":
                return self._patch_ingress(args[2:], dry_run=dry_run)
            if sub in ("deployment", "deploy"):
                return self._patch_deployment(args[2:], dry_run=dry_run)
            if sub in ("service", "svc"):
                return self._patch_service(args[2:], dry_run=dry_run)
        if cmd == "get":
            return self._get(args[1:])
        if cmd == "exec":
            return self._exec(args[1:])
        if cmd == "logs":
            return self._logs(args[1:])

        supported = "rollout restart/status, scale, delete pod, set image, patch ingress/deployment/service, get pods/deployments, exec, logs"
        return False, f"'{cmd}' is not supported in automated mode. Supported: {supported}"

    # ------------------------------------------------------------------
    @staticmethod
    def _parse_ns(args: list, default: str = "default") -> Tuple[str, list]:
        ns = default
        rest: list = []
        i = 0
        while i < len(args):
            if args[i] in ("-n", "--namespace") and i + 1 < len(args):
                ns = args[i + 1]; i += 2
            elif args[i].startswith("-n") and len(args[i]) > 2:
                ns = args[i][2:]; i += 1
            elif args[i].startswith("--namespace="):
                ns = args[i].split("=", 1)[1]; i += 1
            else:
                rest.append(args[i]); i += 1
        return ns, rest

    @staticmethod
    def _parse_resource(args: list, supported: str = "deployment") -> Tuple[Optional[str], list]:
        """Extract resource name from 'deployment <name>' or 'deployment/<name>' forms.
        Returns (name, remaining_args) or (None, []) on parse failure."""
        if not args:
            return None, []
        first = args[0]
        if "/" in first:
            rtype, name = first.split("/", 1)
            if rtype.lower() != supported:
                return None, []
            return name, args[1:]
        if first.lower() == supported:
            return None, args[1:]  # sentinel: name is in remaining args
        return None, []

    def _rollout_restart(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        if not args:
            return False, "Usage: kubectl rollout restart deployment/<name> -n <ns>"
        first = args[0]
        if "/" in first:
            rtype, name = first.split("/", 1)
            if rtype.lower() != "deployment":
                return False, "rollout restart only supports deployments."
            ns, _ = self._parse_ns(args[1:])
        elif first.lower() == "deployment":
            ns, rest = self._parse_ns(args[1:])
            if not rest:
                return False, "Deployment name required."
            name = rest[0]
        else:
            return False, "Usage: kubectl rollout restart deployment/<name> -n <ns>"
        patch = {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": dt.datetime.now(dt.timezone.utc).isoformat()
        }}}}}
        self.apps_v1.patch_namespaced_deployment(name, ns, patch, dry_run="All" if dry_run else None)
        return True, f"{'[dry-run] ' if dry_run else ''}deployment.apps/{name} restarted"

    def _rollout_status(self, args: list) -> Tuple[bool, str]:
        if not args:
            return False, "Usage: kubectl rollout status deployment/<name> -n <ns>"
        first = args[0]
        if "/" in first:
            rtype, name = first.split("/", 1)
            if rtype.lower() != "deployment":
                return False, "rollout status only supports deployments."
            ns, _ = self._parse_ns(args[1:])
        elif first.lower() == "deployment":
            ns, rest = self._parse_ns(args[1:])
            if not rest:
                return False, "Deployment name required."
            name = rest[0]
        else:
            return False, "Usage: kubectl rollout status deployment/<name> -n <ns>"
        dep = self.apps_v1.read_namespaced_deployment(name, ns)
        desired = dep.spec.replicas or 1
        ready = dep.status.ready_replicas or 0
        if ready >= desired:
            return True, f'deployment "{name}" successfully rolled out ({ready}/{desired} ready)'
        return False, f'deployment "{name}" not yet ready ({ready}/{desired} ready)'

    def _scale(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        replicas: Optional[int] = None
        clean: list = []
        i = 0
        while i < len(args):
            if args[i].startswith("--replicas="):
                replicas = int(args[i].split("=", 1)[1]); i += 1
            elif args[i] == "--replicas" and i + 1 < len(args):
                replicas = int(args[i + 1]); i += 2
            else:
                clean.append(args[i]); i += 1
        if replicas is None:
            return False, "Usage: kubectl scale deployment <name> --replicas=N -n <ns>"
        if not clean or clean[0].lower() != "deployment":
            return False, "scale only supports 'deployment'."
        ns, rest = self._parse_ns(clean[1:])
        if not rest:
            return False, "Deployment name required."
        name = rest[0]
        self.apps_v1.patch_namespaced_deployment(
            name, ns, {"spec": {"replicas": replicas}}, dry_run="All" if dry_run else None
        )
        return True, f"{'[dry-run] ' if dry_run else ''}deployment.apps/{name} scaled to {replicas}"

    def _delete(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        if not args or args[0].lower() not in ("pod", "pods", "po"):
            return False, "Automated delete only supports pods. Usage: kubectl delete pod <name> -n <ns>"
        ns, rest = self._parse_ns(args[1:])
        if not rest:
            return False, "Pod name required."
        name = rest[0]
        self.core_v1.delete_namespaced_pod(name, ns, dry_run="All" if dry_run else None)
        return True, f"{'[dry-run] ' if dry_run else ''}pod/{name} deleted"

    def _set_image(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        if not args or "/" not in args[0]:
            return False, "Usage: kubectl set image deployment/<name> <container>=<image> -n <ns>"
        resource = args[0]
        ns, rest = self._parse_ns(args[1:])
        if not rest or "=" not in rest[0]:
            return False, "Container=image spec required."
        rtype, rname = resource.split("/", 1)
        if rtype.lower() != "deployment":
            return False, "set image only supports deployments."
        container_name, new_image = rest[0].split("=", 1)
        dep = self.apps_v1.read_namespaced_deployment(rname, ns)
        for c in dep.spec.template.spec.containers:
            if c.name == container_name:
                c.image = new_image
                break
        else:
            return False, f"Container '{container_name}' not found in deployment '{rname}'."
        self.apps_v1.patch_namespaced_deployment(rname, ns, dep, dry_run="All" if dry_run else None)
        return True, f"{'[dry-run] ' if dry_run else ''}deployment.apps/{rname} image updated ({container_name}={new_image})"

    def _create_service(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        """kubectl create service externalname <name> --external-name <fqdn> -n <ns>"""
        if not args or args[0].lower() != "externalname":
            return False, "Only 'create service externalname' is supported."
        external_name: Optional[str] = None
        clean: list = []
        i = 1
        while i < len(args):
            if args[i].startswith("--external-name="):
                external_name = args[i].split("=", 1)[1]; i += 1
            elif args[i] == "--external-name" and i + 1 < len(args):
                external_name = args[i + 1]; i += 2
            else:
                clean.append(args[i]); i += 1
        ns, rest = self._parse_ns(clean)
        if not rest:
            return False, "Service name required."
        if not external_name:
            return False, "--external-name <fqdn> is required."
        name = rest[0]
        body = k8s_client.V1Service(
            metadata=k8s_client.V1ObjectMeta(name=name, namespace=ns),
            spec=k8s_client.V1ServiceSpec(
                type="ExternalName",
                external_name=external_name,
            ),
        )
        try:
            self.core_v1.create_namespaced_service(ns, body, dry_run="All" if dry_run else None)
            return True, f"{'[dry-run] ' if dry_run else ''}service/{name} created (ExternalName → {external_name})"
        except k8s_client.exceptions.ApiException as exc:
            if exc.status == 409:
                return False, f"service/{name} already exists in {ns}"
            raise

    def _patch_ingress(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        """kubectl patch ingress <name> -n <ns> --type=json -p '<json-patch>'"""
        patch_body: Optional[str] = None
        clean: list = []
        i = 0
        while i < len(args):
            if args[i] in ("-p", "--patch") and i + 1 < len(args):
                patch_body = args[i + 1]; i += 2
            elif args[i].startswith("-p="):
                patch_body = args[i].split("=", 1)[1]; i += 1
            elif args[i] in ("--type=json", "--type", "json"):
                i += 1 if args[i].startswith("--type=") or args[i] == "json" else 2
            else:
                clean.append(args[i]); i += 1
        ns, rest = self._parse_ns(clean)
        if not rest:
            return False, "Ingress name required."
        if not patch_body:
            return False, "-p '<json-patch>' is required."
        name = rest[0]
        try:
            ops = json.loads(patch_body)
        except json.JSONDecodeError as exc:
            return False, f"Invalid JSON in -p: {exc}"
        self.networking_v1.patch_namespaced_ingress(
            name=name,
            namespace=ns,
            body=ops,
            _content_type="application/json-patch+json",
            dry_run="All" if dry_run else None,
        )
        return True, f"{'[dry-run] ' if dry_run else ''}ingress.networking.k8s.io/{name} patched"

    def _patch_deployment(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        """kubectl patch deployment <name> -n <ns> [--type=json|strategic|merge] -p '<patch>'"""
        patch_body: Optional[str] = None
        patch_type = "json"
        clean: list = []
        i = 0
        while i < len(args):
            if args[i] in ("-p", "--patch") and i + 1 < len(args):
                patch_body = args[i + 1]; i += 2
            elif args[i].startswith("-p="):
                patch_body = args[i].split("=", 1)[1]; i += 1
            elif args[i].startswith("--type="):
                patch_type = args[i].split("=", 1)[1].lower(); i += 1
            elif args[i] == "--type" and i + 1 < len(args):
                patch_type = args[i + 1].lower(); i += 2
            else:
                clean.append(args[i]); i += 1
        ns, rest = self._parse_ns(clean)
        if not rest:
            return False, "Deployment name required."
        if not patch_body:
            return False, "-p '<patch>' is required."
        name = rest[0]
        try:
            ops = json.loads(patch_body)
        except json.JSONDecodeError as exc:
            return False, f"Invalid JSON in -p: {exc}"
        content_type = {
            "json":      "application/json-patch+json",
            "merge":     "application/merge-patch+json",
            "strategic": "application/strategic-merge-patch+json",
        }.get(patch_type, "application/json-patch+json")
        self.apps_v1.patch_namespaced_deployment(
            name=name,
            namespace=ns,
            body=ops,
            _content_type=content_type,
            dry_run="All" if dry_run else None,
        )
        return True, f"{'[dry-run] ' if dry_run else ''}deployment.apps/{name} patched"

    def _patch_service(self, args: list, dry_run: bool = False) -> Tuple[bool, str]:
        """kubectl patch service <name> -n <ns> [--type=json|strategic|merge] -p '<patch>'"""
        patch_body: Optional[str] = None
        patch_type = "json"
        clean: list = []
        i = 0
        while i < len(args):
            if args[i] in ("-p", "--patch") and i + 1 < len(args):
                patch_body = args[i + 1]; i += 2
            elif args[i].startswith("-p="):
                patch_body = args[i].split("=", 1)[1]; i += 1
            elif args[i].startswith("--type="):
                patch_type = args[i].split("=", 1)[1].lower(); i += 1
            elif args[i] == "--type" and i + 1 < len(args):
                patch_type = args[i + 1].lower(); i += 2
            else:
                clean.append(args[i]); i += 1
        ns, rest = self._parse_ns(clean)
        if not rest:
            return False, "Service name required."
        if not patch_body:
            return False, "-p '<patch>' is required."
        name = rest[0]
        try:
            ops = json.loads(patch_body)
        except json.JSONDecodeError as exc:
            return False, f"Invalid JSON in -p: {exc}"
        content_type = {
            "json":      "application/json-patch+json",
            "merge":     "application/merge-patch+json",
            "strategic": "application/strategic-merge-patch+json",
        }.get(patch_type, "application/json-patch+json")
        self.core_v1.patch_namespaced_service(
            name=name,
            namespace=ns,
            body=ops,
            _content_type=content_type,
            dry_run="All" if dry_run else None,
        )
        return True, f"{'[dry-run] ' if dry_run else ''}service/{name} patched"

    def _get(self, args: list) -> Tuple[bool, str]:
        if not args:
            return False, "Usage: kubectl get pods|deployments [-n <ns>]"
        ns, _ = self._parse_ns(args[1:])
        rtype = args[0].lower()
        if rtype in ("pod", "pods", "po"):
            pods = self.core_v1.list_namespaced_pod(ns)
            lines = ["NAME\tSTATUS\tRESTARTS"]
            for p in pods.items:
                restarts = sum(cs.restart_count for cs in (p.status.container_statuses or []))
                lines.append(f"{p.metadata.name}\t{p.status.phase or '?'}\t{restarts}")
            return True, "\n".join(lines)
        if rtype in ("deployment", "deployments", "deploy"):
            deps = self.apps_v1.list_namespaced_deployment(ns)
            lines = ["NAME\tREADY\tAVAILABLE"]
            for d in deps.items:
                r = d.status.ready_replicas or 0
                des = d.spec.replicas or 1
                avail = d.status.available_replicas or 0
                lines.append(f"{d.metadata.name}\t{r}/{des}\t{avail}")
            return True, "\n".join(lines)
        return False, f"get does not support '{rtype}'. Supported: pods, deployments"

    def _exec(self, args: list) -> Tuple[bool, str]:
        """kubectl exec <pod> [-n <ns>] -- <command...>"""
        try:
            sep = args.index("--")
            pre, cmd_args = args[:sep], args[sep + 1:]
        except ValueError:
            return False, "Usage: kubectl exec <pod> -n <ns> -- <command...>"
        if not cmd_args:
            return False, "No command specified after --"
        ns, rest = self._parse_ns(pre)
        if not rest:
            return False, "Pod name required."
        pod_name = rest[0]
        try:
            ws = k8s_stream(
                self.core_v1.connect_get_namespaced_pod_exec,
                pod_name, ns,
                command=cmd_args,
                stderr=True, stdin=False, stdout=True, tty=False,
                _preload_content=False,
            )
            ws.run_forever(timeout=30)
            output = ws.read_all() or "(no output)"
            ws.close()
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return True, output[:3000]
        except ApiException as exc:
            return False, f"exec failed ({exc.status}): {exc.reason}"

    def _logs(self, args: list) -> Tuple[bool, str]:
        """kubectl logs <pod> [-n <ns>] [--previous] [--tail=N] [-c <container>]"""
        previous = False
        tail_lines = 80
        container: Optional[str] = None
        clean: list = []
        i = 0
        while i < len(args):
            if args[i] in ("--previous", "-p"):
                previous = True; i += 1
            elif args[i].startswith("--tail="):
                tail_lines = int(args[i].split("=", 1)[1]); i += 1
            elif args[i] == "--tail" and i + 1 < len(args):
                tail_lines = int(args[i + 1]); i += 2
            elif args[i] in ("-c", "--container") and i + 1 < len(args):
                container = args[i + 1]; i += 2
            else:
                clean.append(args[i]); i += 1
        ns, rest = self._parse_ns(clean)
        if not rest:
            return False, "Pod name required."
        pod_name = rest[0]
        try:
            kwargs: Dict[str, Any] = {"previous": previous, "tail_lines": tail_lines}
            if container:
                kwargs["container"] = container
            log = self.core_v1.read_namespaced_pod_log(pod_name, ns, **kwargs)
            if isinstance(log, bytes):
                log = log.decode("utf-8", errors="replace")
            return True, (log[:3000] if len(log) > 3000 else log) or "(no logs)"
        except ApiException as exc:
            return False, f"logs failed ({exc.status}): {exc.reason}"


# ---------------------------------------------------------------------------
# Main agent loop
# ---------------------------------------------------------------------------
_ALERT_COOLDOWN_SECONDS = 3600  # suppress repeat alerts for the same issue within 1 hour


def _load_cooldown_map(core_v1: k8s_client.CoreV1Api, namespace: str) -> Dict[str, float]:
    """Read alert cooldown timestamps from the claude-agent-cooldown ConfigMap."""
    try:
        cm = core_v1.read_namespaced_config_map("claude-agent-cooldown", namespace)
        result: Dict[str, float] = {}
        for k, v in (cm.data or {}).items():
            try:
                result[k] = float(v)
            except ValueError:
                pass
        return result
    except ApiException as exc:
        if exc.status == 404:
            return {}
        logger.warning("Could not read cooldown ConfigMap: %s", exc)
        return {}


def _save_cooldown_map(
    core_v1: k8s_client.CoreV1Api, namespace: str, cooldown: Dict[str, float]
) -> None:
    """Persist alert cooldown timestamps to the claude-agent-cooldown ConfigMap."""
    data = {k: str(v) for k, v in cooldown.items()}
    try:
        try:
            core_v1.read_namespaced_config_map("claude-agent-cooldown", namespace)
            core_v1.patch_namespaced_config_map(
                "claude-agent-cooldown", namespace, {"data": data}
            )
        except ApiException as exc:
            if exc.status == 404:
                core_v1.create_namespaced_config_map(
                    namespace,
                    k8s_client.V1ConfigMap(
                        metadata=k8s_client.V1ObjectMeta(name="claude-agent-cooldown"),
                        data=data,
                    ),
                )
            else:
                raise
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning("Could not save cooldown ConfigMap: %s", exc)


def run_health_check(config: Config) -> None:
    """Execute one full health-check cycle."""
    logger.info("Starting health-check cycle")

    checker = HealthChecker(config)
    collector = ContextCollector(config)
    analyzer = ClaudeAnalyzer(config)
    notifier = TeamsNotifier(config)
    core_v1 = k8s_client.CoreV1Api()

    # Load per-issue alert cooldown so recurring issues (e.g. Helm-reverted hostname
    # conflicts) don't spam Teams every 5 minutes.
    agent_ns = config.agent_namespace
    cooldown_map = _load_cooldown_map(core_v1, agent_ns)
    now_ts = time.time()
    cooldown_updated = False

    # GitOps-first remediation for Helm-owned drift (opt-in per ingress, empty by default).
    gitops_repo_map = gitops.load_repo_map(config.gitops_repo_map_json)

    # 1. Discover all stage services
    services = checker.discover_stage_services()
    logger.info("Discovered %d services in stage namespace(s): %s", len(services), config.namespaces)

    unhealthy_count = 0

    for svc in services:
        logger.info("Checking service: %s/%s", svc["namespace"], svc["name"])

        # 2. Check health
        health_report = checker.check_service_health(svc)

        if health_report["healthy"]:
            logger.info("  ✓ %s is healthy", svc["name"])
            continue

        unhealthy_count += 1
        logger.warning("  ✗ %s is UNHEALTHY: %s", svc["name"], health_report["summary"])

        # Tag resource type so the Teams card can show it.
        if svc.get("_statefulset"):
            svc["resource_type"] = "StatefulSet"
        elif svc.get("_deployment"):
            _svc_keywords = ("targetport", "endpoint", "selector", "service '", "port ")
            _issues_lower = " ".join(health_report.get("issues", [])).lower()
            if any(kw in _issues_lower for kw in _svc_keywords):
                svc["resource_type"] = "Service"
            else:
                svc["resource_type"] = "Deployment"
        else:
            svc["resource_type"] = "Service"

        # Cooldown: suppress repeat alerts for the same service within 1 hour so a
        # persistent crash loop doesn't spam Teams every 5 minutes.
        cooldown_key = f"{svc['namespace']}.{svc['name']}"
        last_alerted = cooldown_map.get(cooldown_key, 0.0)
        if now_ts - last_alerted < _ALERT_COOLDOWN_SECONDS:
            remaining = int(_ALERT_COOLDOWN_SECONDS - (now_ts - last_alerted))
            logger.info(
                "  ⏭ Skipping alert for %s — already alerted %dm ago (cooldown %dm, %dm remaining).",
                svc["name"],
                int((now_ts - last_alerted) / 60),
                _ALERT_COOLDOWN_SECONDS // 60,
                remaining // 60,
            )
            continue

        try:
            # 3. Collect rich context
            context = collector.collect_context(svc, health_report)

            # 4. Analyze with Claude
            analysis = analyzer.analyze_with_claude(context)

            # 5. Send Teams alert and poll for replies
            alert_sent_ts = time.time()
            message_id = notifier.send_to_teams(svc, health_report, analysis)

            # Record alert time so subsequent cycles skip this service for 1 hour.
            if message_id:
                cooldown_map[cooldown_key] = now_ts
                cooldown_updated = True

            if message_id and config.teams_reply_timeout > 0:
                def handle_reply(
                    reply,
                    _ctx=context,
                    _analysis=analysis,
                    _svc=svc,
                    _checker=checker,
                    _mid=message_id,
                    _alert_ts=alert_sent_ts,
                ):
                    author = reply["author"]
                    text = reply["text"]
                    logger.info("Processing reply from %s: %s", author, text[:200])

                    kubectl_cmd = _extract_kubectl_command(text)
                    if kubectl_cmd:
                        logger.info("Detected kubectl command: %s", kubectl_cmd)
                        executor = CommandExecutor()
                        success, output = execute_with_policy(kubectl_cmd, executor)
                        notifier.post_command_card(_mid, kubectl_cmd, output, success)
                        logger.info("Command result (%s): %s", "ok" if success else "err", output)

                        if success and _is_mutating(kubectl_cmd):
                            # Wait for the change to propagate, then re-check health
                            wait = 30
                            logger.info("Waiting %ds for rollout to propagate…", wait)
                            time.sleep(wait)
                            # Refresh the cached deployment object so ready-replica counts are current
                            try:
                                _svc["_deployment"] = _checker.apps_v1.read_namespaced_deployment(
                                    _svc["name"], _svc["namespace"]
                                )
                            except Exception:
                                pass
                            updated_report = _checker.check_service_health(_svc)
                            resolved = updated_report["healthy"]
                            if resolved:
                                logger.info("Service %s is now healthy — sending resolution card", _svc["name"])
                                notifier.send_resolution_reply(_mid, _svc["name"], kubectl_cmd, output)
                            else:
                                summary = updated_report.get("summary", "still unhealthy")
                                notifier.post_status_card(_mid, _svc["name"], still_unhealthy=True, summary=summary)

                            # Persist the outcome so future incidents on this service can be
                            # grounded with what was tried before and whether it worked.
                            hypothesis = (_analysis.get("root_causes") or [{}])[0].get("hypothesis")
                            save_outcome(core_v1, agent_ns, {
                                "service": _svc["name"],
                                "namespace": _svc["namespace"],
                                "hypothesis": hypothesis,
                                "command": kubectl_cmd,
                                "resolved": resolved,
                                "ttr_seconds": int(time.time() - _alert_ts),
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                            })
                    else:
                        followup = analyzer.analyze_followup(
                            _ctx, _analysis, text, author,
                        )
                        notifier.post_followup_card(_mid, followup)
                        logger.info("Posted Claude follow-up card to thread %s", _mid)

                notifier.poll_for_replies(message_id, on_reply=handle_reply)

        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Failed to process unhealthy service %s: %s",
                svc["name"],
                exc,
                exc_info=True,
            )

    # --- Ingress health checks ---
    ingresses = checker.discover_stage_ingresses()
    logger.info("Discovered %d ingresses in namespace(s): %s", len(ingresses), config.namespaces)

    # Track hostnames patched in this cycle so subsequent ingresses sharing the same
    # hostname don't re-alert after the first one was already fixed.
    patched_hostnames: set = set()

    for ing_svc in ingresses:
        logger.info("Checking ingress: %s/%s", ing_svc["namespace"], ing_svc["name"])
        health_report = checker.check_ingress_health(ing_svc)

        if health_report["healthy"]:
            logger.info("  ✓ ingress %s is healthy", ing_svc["name"])
            continue

        # If every conflicting hostname in this ingress was already patched earlier
        # in this cycle, skip — the live state is now clean, only the cached map is stale.
        conflict_hosts = set(list(ing_svc.get("_duplicate_hosts", {}).keys()))
        if conflict_hosts and conflict_hosts.issubset(patched_hostnames):
            logger.info(
                "  ⏭ ingress %s — conflict hostname(s) %s already patched this cycle, skipping",
                ing_svc["name"], conflict_hosts,
            )
            continue

        unhealthy_count += 1
        logger.warning("  ✗ ingress %s is UNHEALTHY: %s", ing_svc["name"], health_report["summary"])
        ing_svc["resource_type"] = "Ingress"

        # Cooldown: skip alert if the same ingress+summary was already alerted within 1 hour.
        # This prevents spam when Helm/GitOps reverts a kubectl patch on each reconcile cycle.
        # ConfigMap keys must match [-._a-zA-Z0-9]+ so replace '/' with '.'.
        cooldown_key = f"{ing_svc['namespace']}.{ing_svc['name']}"
        last_alerted = cooldown_map.get(cooldown_key, 0.0)
        if now_ts - last_alerted < _ALERT_COOLDOWN_SECONDS:
            remaining = int(_ALERT_COOLDOWN_SECONDS - (now_ts - last_alerted))
            logger.info(
                "  ⏭ Skipping alert for %s — already alerted %dm ago (cooldown %dm, %dm remaining). "
                "Root cause likely requires a Helm chart values fix.",
                ing_svc["name"],
                int((now_ts - last_alerted) / 60),
                _ALERT_COOLDOWN_SECONDS // 60,
                remaining // 60,
            )
            continue

        try:
            context = collector.collect_context(ing_svc, health_report)
            analysis = analyzer.analyze_with_claude(context)

            # GitOps-first remediation: if this ingress has a configured chart mapping and
            # Claude suggested a hostname-replace patch, open a durable PR instead of (or as
            # well as) offering a live kubectl patch that Helm would just revert.
            gitops_pr_url = None
            mapping = gitops.lookup_mapping(gitops_repo_map, ing_svc["namespace"], ing_svc["name"])
            if mapping:
                new_host = gitops.extract_target_hostname(analysis)
                if new_host:
                    gitops_pr_url, gitops_error = gitops.open_hostname_pr(
                        config.github_token, mapping, ing_svc["namespace"], ing_svc["name"],
                        new_host, analysis.get("summary", ""),
                    )
                    if gitops_error:
                        logger.warning(
                            "GitOps PR attempt failed for %s/%s: %s",
                            ing_svc["namespace"], ing_svc["name"], gitops_error,
                        )
                    else:
                        logger.info(
                            "GitOps PR ready for %s/%s: %s",
                            ing_svc["namespace"], ing_svc["name"], gitops_pr_url,
                        )

            message_id = notifier.send_to_teams(ing_svc, health_report, analysis, gitops_pr_url=gitops_pr_url)

            # Record alert time so subsequent cycles skip this ingress for 1 hour.
            cooldown_map[cooldown_key] = now_ts
            cooldown_updated = True

            # Wait for a human Teams reply before running any remediation command —
            # mirrors the approval gate service/deployment remediation already uses.
            # (Previously this ran the top-ranked remediation_steps command unconditionally
            # with no human in the loop — the one unattended-mutation path in the codebase.)
            if message_id and config.teams_reply_timeout > 0:
                def handle_ingress_reply(
                    reply,
                    _ctx=context,
                    _analysis=analysis,
                    _mid=message_id,
                    _ing_svc=ing_svc,
                ):
                    author = reply["author"]
                    text = reply["text"]
                    logger.info("Processing ingress reply from %s: %s", author, text[:200])
                    kubectl_cmd = _extract_kubectl_command(text)
                    if kubectl_cmd:
                        executor = CommandExecutor()
                        success, output = execute_with_policy(kubectl_cmd, executor)
                        notifier.post_command_card(_mid, kubectl_cmd, output, success)
                        logger.info("Command result (%s): %s", "ok" if success else "err", output)
                        if success:
                            patched_hostnames.update(list(_ing_svc.get("_duplicate_hosts", {}).keys()))
                    else:
                        followup = analyzer.analyze_followup(_ctx, _analysis, text, author)
                        notifier.post_followup_card(_mid, followup)

                notifier.poll_for_replies(message_id, on_reply=handle_ingress_reply)

        except Exception as exc:  # pylint: disable=broad-except
            logger.error(
                "Failed to process unhealthy ingress %s: %s",
                ing_svc["name"], exc, exc_info=True,
            )

    if cooldown_updated:
        # Prune expired entries before saving to keep the ConfigMap small.
        cooldown_map = {k: v for k, v in cooldown_map.items()
                        if now_ts - v < _ALERT_COOLDOWN_SECONDS}
        _save_cooldown_map(core_v1, agent_ns, cooldown_map)

    logger.info(
        "Health-check cycle complete. %d unhealthy service(s) found.",
        unhealthy_count,
    )


def run_test_alert(config: Config) -> None:
    """Send a synthetic Teams alert — bypasses real health checks."""
    logger.info("TEST_ALERT mode: sending synthetic Teams message")
    notifier = TeamsNotifier(config)

    svc = {"namespace": "apps-stg", "name": "test-service", "labels": {}}
    health_report = {
        "healthy": False,
        "summary": "0/2 replicas ready (CrashLoopBackOff)",
        "ready_replicas": 0,
        "desired_replicas": 2,
        "pod_statuses": [
            {"name": "test-service-abc-1", "healthy": False, "reason": "CrashLoopBackOff"},
            {"name": "test-service-abc-2", "healthy": False, "reason": "CrashLoopBackOff"},
        ],
    }
    analysis = {
        "severity": "high",
        "summary": "[TEST] Synthetic alert — verifying Teams notification pipeline",
        "root_causes": [
            {
                "hypothesis": "This is a test alert triggered by TEST_ALERT=true. No real issue.",
                "confidence": "high",
            }
        ],
        "remediation_steps": [
            {
                "priority": 1,
                "description": "No action needed — this is a test alert",
                "kubectl_command": None,
            }
        ],
    }

    message_id = notifier.send_to_teams(svc, health_report, analysis)
    if message_id:
        logger.info("Test alert sent successfully via Graph API (msg id %s)", message_id)
        if config.teams_reply_timeout > 0:
            notifier.poll_for_replies(message_id, on_reply=lambda r: logger.info(
                "Test reply from %s: %s", r["author"], r["text"][:200]
            ))
    else:
        logger.info("Test alert sent successfully via Incoming Webhook")


def main() -> None:
    config = Config()
    config.validate()

    if config.test_alert:
        run_test_alert(config)
    elif config.run_once:
        run_health_check(config)
    else:
        logger.info("Running on %d-second interval", config.interval_seconds)
        while True:
            try:
                run_health_check(config)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Unhandled error in health-check cycle: %s", exc, exc_info=True)
            logger.info("Sleeping %d seconds until next cycle...", config.interval_seconds)
            time.sleep(config.interval_seconds)


if __name__ == "__main__":
    main()
