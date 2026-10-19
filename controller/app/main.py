"""FastAPI application entrypoint for the self-healing controller."""

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from kubernetes import client, config
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from .actions import ACTION_REGISTRY
from .incident_log import log_incident_event
from .metrics import ACTIONS_TOTAL, ALERTS_RECEIVED_TOTAL
from .policy import PolicyEngine
from .rails import SafetyRails
from .schemas import Alert, AlertmanagerWebhook

logger = logging.getLogger("controller")
logging.basicConfig(level=logging.INFO)


class K8sClients:
    """Container for Kubernetes API clients."""

    def __init__(self):
        self.core_v1: Optional[client.CoreV1Api] = None
        self.apps_v1: Optional[client.AppsV1Api] = None

    def initialize(self):
        try:
            config.load_incluster_config()
            self.core_v1 = client.CoreV1Api()
            self.apps_v1 = client.AppsV1Api()
            logger.info("Loaded in-cluster Kubernetes configuration")
        except Exception:
            try:
                config.load_kube_config()
                self.core_v1 = client.CoreV1Api()
                self.apps_v1 = client.AppsV1Api()
                logger.info("Loaded local kubeconfig")
            except Exception as e:
                logger.warning(f"Could not initialize Kubernetes client: {e}. Running in degraded/mock mode.")


k8s_clients = K8sClients()
policy_engine = PolicyEngine()
safety_rails = SafetyRails(policy_engine)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: initialize dependencies."""
    k8s_clients.initialize()
    yield


app = FastAPI(title="Self-Healing Controller", version="1.0.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return 400 Bad Request on malformed Alertmanager payloads."""
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": "Malformed webhook payload", "errors": exc.errors()},
    )


@app.get("/healthz", status_code=status.HTTP_200_OK)
def healthz():
    """Liveness probe: returns 200 without external dependencies."""
    return {"status": "ok"}


@app.get("/readyz", status_code=status.HTTP_200_OK)
def readyz():
    """Readiness probe: returns 200."""
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    """Prometheus metrics scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


def _process_alert_sync(alert: Alert, rails: SafetyRails, k8s: K8sClients):
    """Synchronous worker that evaluates safety rails and executes approved actions."""
    incident_id = f"{alert.alertname}:{alert.service}:{alert.startsAt}"
    ALERTS_RECEIVED_TOTAL.labels(alertname=alert.alertname, status=alert.status).inc()

    # Log incoming alert
    log_incident_event(
        event="alert_received",
        incident_id=incident_id,
        alertname=alert.alertname,
        service=alert.service,
        pod=alert.pod,
        severity=alert.severity,
        fingerprint=alert.fingerprint,
        starts_at=alert.startsAt,
        dry_run=rails.dry_run,
    )

    decision = rails.evaluate(alert, k8s)

    if decision.decision == "resolve":
        log_incident_event(
            event="incident_resolved",
            incident_id=incident_id,
            alertname=alert.alertname,
            service=alert.service,
            pod=alert.pod,
            severity=alert.severity,
            fingerprint=alert.fingerprint,
            starts_at=alert.startsAt,
            decision=decision.decision,
            reason=decision.reason,
            duration_ms=decision.duration_ms,
            dry_run=rails.dry_run,
        )
        return

    # Log policy decision
    log_incident_event(
        event="decision",
        incident_id=incident_id,
        alertname=alert.alertname,
        service=alert.service,
        pod=alert.pod,
        severity=alert.severity,
        fingerprint=alert.fingerprint,
        starts_at=alert.startsAt,
        decision=decision.decision,
        reason=decision.reason,
        action=decision.action,
        dry_run=rails.dry_run,
        extra={"attempt": decision.attempt, "needs_human": decision.needs_human},
    )

    if decision.decision == "escalate":
        logger.error(
            f"Incident {incident_id} escalated: circuit breaker tripped (3 actions/hr limit exceeded). Human intervention required."
        )
        return

    if not decision.allowed:
        return

    # Action is approved
    action_impl = ACTION_REGISTRY.get(decision.action or "")
    if not action_impl:
        logger.warning(f"No action implementation registered for {decision.action}")
        return

    if decision.action == "log_only":
        outcome = action_impl.execute(alert, k8s, dry_run=decision.dry_run)
        ACTIONS_TOTAL.labels(action="log_only", outcome=outcome).inc()
        log_incident_event(
            event="action_finished",
            incident_id=incident_id,
            alertname=alert.alertname,
            service=alert.service,
            pod=alert.pod,
            severity=alert.severity,
            fingerprint=alert.fingerprint,
            starts_at=alert.startsAt,
            decision=decision.decision,
            reason=decision.reason,
            action=decision.action,
            outcome=outcome,
            dry_run=rails.dry_run,
        )
        return

    # Mutating cluster action (rollout_restart or restart_pod)
    rails.set_in_flight(True)
    start_time = time.time()
    try:
        log_incident_event(
            event="action_started",
            incident_id=incident_id,
            alertname=alert.alertname,
            service=alert.service,
            pod=alert.pod,
            severity=alert.severity,
            fingerprint=alert.fingerprint,
            starts_at=alert.startsAt,
            action=decision.action,
            dry_run=rails.dry_run,
            extra={"attempt": decision.attempt},
        )

        outcome = action_impl.execute(alert, k8s, dry_run=decision.dry_run)

        # Verification step if not dry-run and not already absent
        if not decision.dry_run and outcome not in ("pod_gone", "deployment_not_found"):
            verified = action_impl.verify(alert, k8s, timeout_sec=120)
            if not verified:
                outcome = f"{outcome}_unverified"

        elapsed_ms = (time.time() - start_time) * 1000.0
        ACTIONS_TOTAL.labels(action=decision.action, outcome=outcome).inc()

        log_incident_event(
            event="action_finished",
            incident_id=incident_id,
            alertname=alert.alertname,
            service=alert.service,
            pod=alert.pod,
            severity=alert.severity,
            fingerprint=alert.fingerprint,
            starts_at=alert.startsAt,
            action=decision.action,
            outcome=outcome,
            duration_ms=elapsed_ms,
            dry_run=rails.dry_run,
            extra={"attempt": decision.attempt},
        )
    finally:
        rails.set_in_flight(False)


@app.post("/webhook", status_code=status.HTTP_200_OK)
async def webhook(payload: AlertmanagerWebhook):
    """Alertmanager v4 webhook receiver."""
    # Process alerts asynchronously to respond to Alertmanager well under HTTP timeout
    for alert in payload.alerts:
        asyncio.create_task(asyncio.to_thread(_process_alert_sync, alert, safety_rails, k8s_clients))

    return {"status": "accepted", "alerts_count": len(payload.alerts)}
