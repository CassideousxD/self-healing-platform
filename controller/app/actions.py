"""Pluggable remediation actions for the self-healing controller."""

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Dict

from kubernetes.client.rest import ApiException

from .schemas import Alert


class BaseAction(ABC):
    """Abstract base class for all controller actions."""

    name: str = "base"

    @abstractmethod
    def execute(self, alert: Alert, k8s_api: Any, dry_run: bool = True) -> str:
        """Execute the remediation action and return an outcome string."""
        pass

    @abstractmethod
    def verify(self, alert: Alert, k8s_api: Any, timeout_sec: int = 120) -> bool:
        """Verify the remediation action completed successfully."""
        pass


class LogOnlyAction(BaseAction):
    """Passive action that records the alert without cluster mutations."""

    name: str = "log_only"

    def execute(self, alert: Alert, k8s_api: Any, dry_run: bool = True) -> str:
        return "logged"

    def verify(self, alert: Alert, k8s_api: Any, timeout_sec: int = 120) -> bool:
        return True


class RestartPodAction(BaseAction):
    """Deletes a crash-looping pod to force a clean container recreation."""

    name: str = "restart_pod"

    def execute(self, alert: Alert, k8s_api: Any, dry_run: bool = True) -> str:
        pod_name = alert.pod
        namespace = alert.namespace or "platform"
        if not pod_name:
            return "missing_pod"

        if dry_run:
            return "dry_run"

        core_v1 = getattr(k8s_api, "core_v1", None)
        if core_v1 is None:
            return "no_k8s_client"

        try:
            core_v1.delete_namespaced_pod(name=pod_name, namespace=namespace, _request_timeout=(2, 5))
            return "pod_deleted"
        except ApiException as e:
            if e.status == 404:
                # Pod was already terminated / evicted
                return "pod_gone"
            raise

    def verify(self, alert: Alert, k8s_api: Any, timeout_sec: int = 120) -> bool:
        pod_name = alert.pod
        service = alert.service
        namespace = alert.namespace or "platform"
        core_v1 = getattr(k8s_api, "core_v1", None)
        if core_v1 is None:
            return True

        deadline = time.time() + timeout_sec
        selector = f"app={service}" if service else None

        while time.time() < deadline:
            try:
                pods = core_v1.list_namespaced_pod(
                    namespace=namespace,
                    label_selector=selector,
                    _request_timeout=(2, 5),
                )
                for p in pods.items:
                    # Look for a different running & ready pod for the service
                    if p.metadata.name != pod_name and p.status.phase == "Running":
                        statuses = p.status.container_statuses or []
                        if statuses and all(s.ready for s in statuses):
                            return True
            except Exception:
                pass
            time.sleep(2)

        return False


class RolloutRestartAction(BaseAction):
    """Executes a rolling restart of an allowlisted Deployment."""

    name: str = "rollout_restart"

    def execute(self, alert: Alert, k8s_api: Any, dry_run: bool = True) -> str:
        service = alert.service
        namespace = alert.namespace or "platform"
        if not service:
            return "missing_service"

        if dry_run:
            return "dry_run"

        apps_v1 = getattr(k8s_api, "apps_v1", None)
        if apps_v1 is None:
            return "no_k8s_client"

        now_iso = datetime.now(timezone.utc).isoformat()
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now_iso,
                        }
                    }
                }
            }
        }
        try:
            apps_v1.patch_namespaced_deployment(name=service, namespace=namespace, body=patch, _request_timeout=(2, 5))
            return "rollout_started"
        except ApiException as e:
            if e.status == 404:
                return "deployment_not_found"
            raise

    def verify(self, alert: Alert, k8s_api: Any, timeout_sec: int = 120) -> bool:
        service = alert.service
        namespace = alert.namespace or "platform"
        apps_v1 = getattr(k8s_api, "apps_v1", None)
        if apps_v1 is None:
            return True

        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                dep = apps_v1.read_namespaced_deployment(name=service, namespace=namespace, _request_timeout=(2, 5))
                spec_replicas = dep.spec.replicas or 1
                status = dep.status
                updated = status.updated_replicas or 0
                ready = status.ready_replicas or 0
                available = status.available_replicas or 0

                if updated >= spec_replicas and ready >= spec_replicas and available >= spec_replicas:
                    return True
            except Exception:
                pass
            time.sleep(2)

        return False


ACTION_REGISTRY: Dict[str, BaseAction] = {
    "log_only": LogOnlyAction(),
    "restart_pod": RestartPodAction(),
    "rollout_restart": RolloutRestartAction(),
}
