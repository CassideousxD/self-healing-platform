"""Safety rails, incident tracking, rate limits, and anti-spoofing verification."""

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from kubernetes.client.rest import ApiException

from .metrics import DECISIONS_TOTAL, INCIDENTS_OPEN
from .policy import PolicyEngine
from .schemas import Alert

logger = logging.getLogger("controller.rails")


@dataclass
class Incident:
    """Tracks active incident identity, remediation attempts, and cooldowns."""

    incident_id: str
    alertname: str
    service: str
    starts_at: str
    created_at: float = field(default_factory=time.time)
    last_action_at: float = 0.0
    attempts: int = 0
    escalated: bool = False
    active: bool = True


@dataclass
class Decision:
    """The remediation decision produced by the safety rails."""

    allowed: bool
    decision: str  # act | skip | refuse | escalate | resolve | ignore
    reason: str
    action: Optional[str] = None
    attempt: int = 0
    needs_human: bool = False
    dry_run: bool = True
    incident_id: str = ""
    duration_ms: float = 0.0


class SafetyRails:
    """Enforces safety rails, rate limits, ownership verification, and circuit breakers."""

    def __init__(self, policy_engine: PolicyEngine):
        self.policy = policy_engine
        self._lock = threading.Lock()
        # Incident identity is keyed by (alertname, service, startsAt)
        self.active_incidents: Dict[Tuple[str, str, str], Incident] = {}
        # Cooldown is keyed on (alertname, service)
        # Note: A second crash-looping pod of the same service inside cooldown is skipped.
        self.service_cooldowns: Dict[Tuple[str, str], float] = {}
        # Sliding-window timestamps of actions executed per service (for circuit breaker)
        self.service_actions: Dict[str, List[float]] = {}
        # Global in-flight cap: at most one remediation action executing at a time cluster-wide
        self.global_in_flight: bool = False
        self.in_flight_since: float = 0.0

    @property
    def dry_run(self) -> bool:
        """Read DRY_RUN env var, defaulting to true."""
        return os.getenv("DRY_RUN", "true").lower() in ("true", "1", "yes")

    def _get_or_create_incident(self, alert: Alert) -> Incident:
        key = (alert.alertname, alert.service, alert.startsAt)
        if key not in self.active_incidents:
            incident_id = f"{alert.alertname}:{alert.service}:{alert.startsAt}"
            inc = Incident(
                incident_id=incident_id,
                alertname=alert.alertname,
                service=alert.service,
                starts_at=alert.startsAt,
            )
            self.active_incidents[key] = inc
            INCIDENTS_OPEN.set(len(self.active_incidents))
        return self.active_incidents[key]

    def _verify_pod_owner(self, pod_name: str, expected_service: str, k8s_api: Any) -> Tuple[bool, str]:
        """Verify that a pod belongs to an allowlisted Deployment via ownerReferences traversal.

        FAIL CLOSED: Never permit a pod action if the Kubernetes API client is unavailable.
        Uses _request_timeout=(2, 5) to guarantee calls never hang indefinitely.
        """
        if k8s_api is None:
            return False, "k8s_client_unavailable"

        core_v1 = getattr(k8s_api, "core_v1", None)
        apps_v1 = getattr(k8s_api, "apps_v1", None)
        if core_v1 is None or apps_v1 is None:
            return False, "k8s_client_unavailable"

        namespace = "platform"
        timeout = (2, 5)  # 2s connect, 5s read
        try:
            pod = core_v1.read_namespaced_pod(name=pod_name, namespace=namespace, _request_timeout=timeout)
        except ApiException as e:
            if e.status == 404:
                return False, "pod_gone"
            return False, f"k8s_error_{e.status}"
        except Exception as e:
            return False, f"error_{str(e)}"

        # Step 1: Pod -> ReplicaSet ownerReference
        owners = pod.metadata.owner_references or []
        rs_name = None
        for owner in owners:
            if owner.kind == "ReplicaSet":
                rs_name = owner.name
                break

        if not rs_name:
            return False, "no_replicaset_owner"

        # Step 2: ReplicaSet -> Deployment ownerReference
        try:
            rs = apps_v1.read_namespaced_replica_set(name=rs_name, namespace=namespace, _request_timeout=timeout)
        except ApiException as e:
            if e.status == 404:
                return False, "replicaset_gone"
            return False, f"k8s_error_{e.status}"
        except Exception as e:
            return False, f"error_{str(e)}"

        rs_owners = rs.metadata.owner_references or []
        dep_name = None
        for owner in rs_owners:
            if owner.kind == "Deployment":
                dep_name = owner.name
                break

        if not dep_name:
            return False, "no_deployment_owner"

        if dep_name != expected_service:
            return False, "owner_deployment_mismatch"

        if not self.policy.is_service_allowed(dep_name):
            return False, "owner_deployment_not_allowed"

        return True, "owner_verified"

    def evaluate(self, alert: Alert, k8s_api: Any = None) -> Decision:
        """Evaluate safety rails, policies, rate limits, and ownership for an incoming alert."""
        incident_id = f"{alert.alertname}:{alert.service}:{alert.startsAt}"

        # 1. Stateless / Filter checks (outside lock)
        if alert.status != "resolved":
            # Check remediation filter: strictly require remediation == "auto"
            if alert.remediation != "auto":
                DECISIONS_TOTAL.labels(decision="refuse").inc()
                return Decision(
                    allowed=False,
                    decision="refuse",
                    reason="not_auto_remediation",
                    incident_id=incident_id,
                    dry_run=self.dry_run,
                )

            # Check namespace filter: strictly require namespace == "platform"
            if alert.namespace != "platform":
                DECISIONS_TOTAL.labels(decision="refuse").inc()
                return Decision(
                    allowed=False,
                    decision="refuse",
                    reason="not_platform_namespace",
                    incident_id=incident_id,
                    dry_run=self.dry_run,
                )

            # Check service allowlist
            if not self.policy.is_service_allowed(alert.service):
                DECISIONS_TOTAL.labels(decision="refuse").inc()
                return Decision(
                    allowed=False,
                    decision="refuse",
                    reason="service_not_allowlisted",
                    incident_id=incident_id,
                    dry_run=self.dry_run,
                )

            # Check action mapping from policy
            action_name = self.policy.get_action_for_alert(alert.alertname)
            if not action_name:
                DECISIONS_TOTAL.labels(decision="ignore").inc()
                return Decision(
                    allowed=False,
                    decision="ignore",
                    reason="unknown_alert",
                    incident_id=incident_id,
                    dry_run=self.dry_run,
                )

            # 2. Pod ownership anti-spoofing verification (OUTSIDE LOCK)
            # Read-only network calls run before acquiring the mutex so K8s latency
            # never blocks concurrent threads evaluating other alerts.
            if action_name == "restart_pod":
                if not alert.pod:
                    DECISIONS_TOTAL.labels(decision="refuse").inc()
                    return Decision(
                        allowed=False,
                        decision="refuse",
                        reason="missing_pod_label",
                        incident_id=incident_id,
                        dry_run=self.dry_run,
                    )
                verified, owner_reason = self._verify_pod_owner(alert.pod, alert.service, k8s_api)
                if not verified:
                    if owner_reason == "pod_gone":
                        # Pod already gone is a no-op with outcome pod_gone
                        DECISIONS_TOTAL.labels(decision="skip").inc()
                        return Decision(
                            allowed=False,
                            decision="skip",
                            reason="pod_gone",
                            action="restart_pod",
                            incident_id=incident_id,
                            dry_run=self.dry_run,
                        )
                    DECISIONS_TOTAL.labels(decision="refuse").inc()
                    return Decision(
                        allowed=False,
                        decision="refuse",
                        reason=owner_reason,
                        incident_id=incident_id,
                        dry_run=self.dry_run,
                    )
        else:
            action_name = None

        # 3. Synchronized State Evaluation (UNDER LOCK)
        with self._lock:
            now = time.time()

            # Handle alert resolution
            if alert.status == "resolved":
                key = (alert.alertname, alert.service, alert.startsAt)
                incident = self.active_incidents.pop(key, None)
                INCIDENTS_OPEN.set(len(self.active_incidents))
                if incident:
                    duration_ms = (now - incident.created_at) * 1000.0
                    DECISIONS_TOTAL.labels(decision="resolve").inc()
                    return Decision(
                        allowed=False,
                        decision="resolve",
                        reason="incident_resolved",
                        incident_id=incident.incident_id,
                        duration_ms=duration_ms,
                        dry_run=self.dry_run,
                    )
                else:
                    DECISIONS_TOTAL.labels(decision="ignore").inc()
                    return Decision(
                        allowed=False,
                        decision="ignore",
                        reason="unknown_incident_resolved",
                        incident_id=incident_id,
                        dry_run=self.dry_run,
                    )

            # If action is log_only, permit immediately without rate limits or K8s mutations
            if action_name == "log_only":
                incident = self._get_or_create_incident(alert)
                DECISIONS_TOTAL.labels(decision="act").inc()
                return Decision(
                    allowed=True,
                    decision="act",
                    reason="log_only_action",
                    action="log_only",
                    attempt=1,
                    incident_id=incident.incident_id,
                    dry_run=self.dry_run,
                )

            # Auto-expire hung or stale global in-flight cap (> 180s)
            if self.global_in_flight and (now - self.in_flight_since >= 180.0):
                logger.error(
                    f"Global in-flight action expired after {now - self.in_flight_since:.1f}s. Auto-releasing."
                )
                self.global_in_flight = False
                self.in_flight_since = 0.0

            # Check Global In-Flight Cap (thundering herd protection across all services)
            if self.global_in_flight:
                # Skipped decisions MUST NOT mark the incident as handled or update cooldown
                DECISIONS_TOTAL.labels(decision="skip").inc()
                return Decision(
                    allowed=False,
                    decision="skip",
                    reason="global_in_flight",
                    action=action_name,
                    incident_id=incident_id,
                    dry_run=self.dry_run,
                )

            # Get or create incident tracker
            incident = self._get_or_create_incident(alert)

            # ORDER OF CHECKS: Cooldown FIRST, then Circuit Breaker.
            # A delivery inside the cooldown is always skip(cooldown_active),
            # even right after the 3rd action.
            cooldown_key = (alert.alertname, alert.service)
            last_action_at = max(
                incident.last_action_at,
                self.service_cooldowns.get(cooldown_key, 0.0),
            )

            if last_action_at > 0.0:
                elapsed = now - last_action_at
                if elapsed < self.policy.cooldown_seconds:
                    DECISIONS_TOTAL.labels(decision="skip").inc()
                    return Decision(
                        allowed=False,
                        decision="skip",
                        reason="cooldown_active",
                        action=action_name,
                        attempt=incident.attempts,
                        incident_id=incident.incident_id,
                        dry_run=self.dry_run,
                    )

            # Cooldown has elapsed: apply Circuit Breaker (sliding 1-hour window per service)
            # Prune actions older than 3600 seconds
            history = self.service_actions.setdefault(alert.service, [])
            self.service_actions[alert.service] = [t for t in history if now - t < 3600.0]
            if len(self.service_actions[alert.service]) >= self.policy.max_actions_per_hour:
                if incident.escalated:
                    # Already escalated: skip without log spam on later repeats
                    DECISIONS_TOTAL.labels(decision="skip").inc()
                    return Decision(
                        allowed=False,
                        decision="skip",
                        reason="already_escalated",
                        action=action_name,
                        attempt=incident.attempts,
                        needs_human=True,
                        incident_id=incident.incident_id,
                        dry_run=self.dry_run,
                    )
                else:
                    incident.escalated = True
                    DECISIONS_TOTAL.labels(decision="escalate").inc()
                    return Decision(
                        allowed=False,
                        decision="escalate",
                        reason="circuit_breaker_tripped",
                        action=action_name,
                        attempt=incident.attempts + 1,
                        needs_human=True,
                        incident_id=incident.incident_id,
                        dry_run=self.dry_run,
                    )

            # Both Cooldown and Circuit Breaker allow action:
            # Set in-flight atomically under lock to block any concurrent actions
            self.global_in_flight = True
            self.in_flight_since = now

            incident.attempts += 1
            incident.last_action_at = now
            self.service_cooldowns[cooldown_key] = now
            self.service_actions[alert.service].append(now)

            DECISIONS_TOTAL.labels(decision="act").inc()
            return Decision(
                allowed=True,
                decision="act",
                reason="ok",
                action=action_name,
                attempt=incident.attempts,
                incident_id=incident.incident_id,
                dry_run=self.dry_run,
            )

    def release_in_flight(self) -> None:
        """Release the global in-flight execution flag."""
        with self._lock:
            self.global_in_flight = False
            self.in_flight_since = 0.0
