"""Unit tests for the self-healing controller, safety rails, policies, and actions."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from controller.app.actions import RestartPodAction, RolloutRestartAction
from controller.app.main import app
from controller.app.policy import PolicyEngine
from controller.app.rails import SafetyRails
from controller.app.schemas import Alert, AlertmanagerWebhook

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def policy():
    """Create a policy engine with clean default test rules."""
    return PolicyEngine()


@pytest.fixture
def rails(policy):
    """Create a fresh SafetyRails instance."""
    return SafetyRails(policy)


@pytest.fixture
def mock_k8s():
    """Create mock K8s API client structure."""
    client_mock = MagicMock()
    client_mock.core_v1 = MagicMock()
    client_mock.apps_v1 = MagicMock()
    return client_mock


def load_fixture(filename: str) -> dict:
    """Load JSON fixture from the fixtures directory."""
    with open(FIXTURES_DIR / filename, "r", encoding="utf-8") as f:
        return json.load(f)


def test_every_fixture_parses():
    """Ensure all 8 Alertmanager v4 fixtures parse cleanly without schema errors."""
    fixture_files = list(FIXTURES_DIR.glob("*.json"))
    assert len(fixture_files) == 8, f"Expected 8 fixture files, found {len(fixture_files)}"

    for fpath in fixture_files:
        with open(fpath, "r", encoding="utf-8") as f:
            raw = f.read()
        parsed = AlertmanagerWebhook.model_validate_json(raw)
        assert parsed.receiver == "echo-receiver"
        assert len(parsed.alerts) >= 1
        for alert in parsed.alerts:
            assert alert.status in ("firing", "resolved")
            assert "alertname" in alert.labels


def test_api_high_error_rate_rollout_restart(rails, mock_k8s):
    """ApiHighErrorRate firing triggers one rollout_restart action on api."""
    data = load_fixture("alertmanager_firing.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    decision = rails.evaluate(alert, mock_k8s)
    assert decision.allowed is True
    assert decision.decision == "act"
    assert decision.action == "rollout_restart"
    assert decision.attempt == 1
    assert alert.service == "api"


def test_redelivery_inside_cooldown_is_noop(rails, mock_k8s):
    """A repeated delivery of the same payload inside the 240s cooldown is skipped."""
    data = load_fixture("alertmanager_firing.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    # First delivery: acts
    d1 = rails.evaluate(alert, mock_k8s)
    assert d1.allowed is True
    assert d1.decision == "act"

    # Immediate second delivery: skipped due to cooldown
    d2 = rails.evaluate(alert, mock_k8s)
    assert d2.allowed is False
    assert d2.decision == "skip"
    assert d2.reason == "cooldown_active"
    assert d2.attempt == 1


def test_repeat_after_cooldown_acts_again(rails, mock_k8s):
    """A repeat delivery after the cooldown has elapsed acts again (attempt 2)."""
    data = load_fixture("alertmanager_firing.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    # Attempt 1
    d1 = rails.evaluate(alert, mock_k8s)
    assert d1.allowed is True
    assert d1.attempt == 1

    # Simulate passage of 250s (greater than 240s cooldown)
    with patch("time.time", return_value=time.time() + 250):
        d2 = rails.evaluate(alert, mock_k8s)
        assert d2.allowed is True
        assert d2.decision == "act"
        assert d2.attempt == 2


def test_circuit_breaker_escalates_on_fourth_action(rails, mock_k8s):
    """The 4th attempt within an hour trips the circuit breaker and escalates once."""
    data = load_fixture("alertmanager_firing.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    base_time = time.time()

    # Attempt 1 (t = 0)
    with patch("time.time", return_value=base_time):
        d1 = rails.evaluate(alert, mock_k8s)
        assert d1.allowed is True
        assert d1.attempt == 1

    # Attempt 2 (t = 300)
    with patch("time.time", return_value=base_time + 300):
        d2 = rails.evaluate(alert, mock_k8s)
        assert d2.allowed is True
        assert d2.attempt == 2

    # Attempt 3 (t = 600)
    with patch("time.time", return_value=base_time + 600):
        d3 = rails.evaluate(alert, mock_k8s)
        assert d3.allowed is True
        assert d3.attempt == 3

    # Attempt 4 (t = 900) -> 3 actions already taken within the hour, trips circuit breaker
    with patch("time.time", return_value=base_time + 900):
        d4 = rails.evaluate(alert, mock_k8s)
        assert d4.allowed is False
        assert d4.decision == "escalate"
        assert d4.reason == "circuit_breaker_tripped"
        assert d4.needs_human is True

    # Attempt 5 (t = 1200) -> Already escalated, skipped without repeated error spam
    with patch("time.time", return_value=base_time + 1200):
        d5 = rails.evaluate(alert, mock_k8s)
        assert d5.allowed is False
        assert d5.decision == "skip"
        assert d5.reason == "already_escalated"


def test_multi_alert_fixture_two_pod_restarted(rails, mock_k8s):
    """The multi-alert fixture yields two PodRestarted decisions (both log_only)."""
    data = load_fixture("alertmanager_multi_alert.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    assert len(webhook.alerts) == 2

    for alert in webhook.alerts:
        assert alert.alertname == "PodRestarted"
        d = rails.evaluate(alert, mock_k8s)
        assert d.allowed is True
        assert d.decision == "act"
        assert d.action == "log_only"


def test_pod_crashlooping_crashtest_refused(rails, mock_k8s):
    """alertmanager_pod_crashlooping.json has service=crashtest and must be refused."""
    data = load_fixture("alertmanager_pod_crashlooping.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]
    assert alert.service == "crashtest"

    decision = rails.evaluate(alert, mock_k8s)
    assert decision.allowed is False
    assert decision.decision == "refuse"
    assert decision.reason == "service_not_allowlisted"


def make_owner_ref(kind: str, name: str) -> MagicMock:
    """Helper to create a Kubernetes OwnerReference mock with explicit attribute assignment."""
    ref = MagicMock()
    ref.kind = kind
    ref.name = name
    return ref


def test_pod_crashlooping_api_allowed_with_owner_verification(rails, mock_k8s):
    """Variant with service=api and pod owned by api Deployment is acted on (restart_pod)."""
    data = load_fixture("alertmanager_pod_crashlooping.json")
    # Derive variant in test code without modifying fixture file
    data["alerts"][0]["labels"]["service"] = "api"
    data["alerts"][0]["labels"]["pod"] = "api-6fb546cd54-abcde"

    # Set up mock ownerReferences: Pod -> ReplicaSet (api-6fb546cd54) -> Deployment (api)
    mock_pod = MagicMock()
    mock_pod.metadata.name = "api-6fb546cd54-abcde"
    mock_pod.metadata.owner_references = [make_owner_ref(kind="ReplicaSet", name="api-6fb546cd54")]

    mock_rs = MagicMock()
    mock_rs.metadata.name = "api-6fb546cd54"
    mock_rs.metadata.owner_references = [make_owner_ref(kind="Deployment", name="api")]

    mock_k8s.core_v1.read_namespaced_pod.return_value = mock_pod
    mock_k8s.apps_v1.read_namespaced_replica_set.return_value = mock_rs

    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    decision = rails.evaluate(alert, mock_k8s)
    assert decision.allowed is True
    assert decision.decision == "act"
    assert decision.action == "restart_pod"


def test_restart_pod_404_is_noop_pod_gone(mock_k8s):
    """When deleting a pod returns 404 (already gone), outcome is pod_gone rather than error."""
    alert = Alert(
        status="firing",
        labels={"alertname": "PodCrashLooping", "service": "api", "pod": "api-old", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    action = RestartPodAction()
    mock_k8s.core_v1.delete_namespaced_pod.side_effect = ApiException(status=404)

    outcome = action.execute(alert, mock_k8s, dry_run=False)
    assert outcome == "pod_gone"


def test_second_crashlooping_pod_of_same_service_in_cooldown_skipped(rails, mock_k8s):
    """A second crash-looping pod of the same service inside cooldown is skipped."""
    mock_pod = MagicMock()
    mock_pod.metadata.owner_references = [make_owner_ref(kind="ReplicaSet", name="api-rs")]
    mock_rs = MagicMock()
    mock_rs.metadata.owner_references = [make_owner_ref(kind="Deployment", name="api")]
    mock_k8s.core_v1.read_namespaced_pod.return_value = mock_pod
    mock_k8s.apps_v1.read_namespaced_replica_set.return_value = mock_rs

    alert1 = Alert(
        status="firing",
        labels={"alertname": "PodCrashLooping", "service": "api", "pod": "api-pod-1", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    d1 = rails.evaluate(alert1, mock_k8s)
    assert d1.allowed is True

    # Pod 2 of same service arrives 30s later (inside 240s cooldown)
    alert2 = Alert(
        status="firing",
        labels={"alertname": "PodCrashLooping", "service": "api", "pod": "api-pod-2", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:30Z",
    )
    with patch("time.time", return_value=time.time() + 30):
        d2 = rails.evaluate(alert2, mock_k8s)
        assert d2.allowed is False
        assert d2.decision == "skip"
        assert d2.reason == "cooldown_active"


def test_resolved_closes_incident_and_clears_state(rails, mock_k8s):
    """Resolved closes the open incident, records duration, and never acts."""
    firing_data = load_fixture("alertmanager_firing.json")
    resolved_data = load_fixture("alertmanager_resolved.json")

    firing_alert = AlertmanagerWebhook.model_validate(firing_data).alerts[0]
    resolved_alert = AlertmanagerWebhook.model_validate(resolved_data).alerts[0]

    # Open incident
    d_fire = rails.evaluate(firing_alert, mock_k8s)
    assert d_fire.allowed is True
    key = (firing_alert.alertname, firing_alert.service, firing_alert.startsAt)
    assert key in rails.active_incidents

    # Deliver resolved
    d_res = rails.evaluate(resolved_alert, mock_k8s)
    assert d_res.allowed is False
    assert d_res.decision == "resolve"
    assert d_res.reason == "incident_resolved"
    assert d_res.duration_ms >= 0.0
    assert key not in rails.active_incidents


def test_resolved_unknown_incident_ignored(rails, mock_k8s):
    """A resolved payload for an untracked incident is safely ignored."""
    resolved_data = load_fixture("alertmanager_resolved.json")
    resolved_alert = AlertmanagerWebhook.model_validate(resolved_data).alerts[0]

    d = rails.evaluate(resolved_alert, mock_k8s)
    assert d.allowed is False
    assert d.decision == "ignore"
    assert d.reason == "unknown_incident_resolved"


def test_global_in_flight_cap(rails, mock_k8s):
    """When an action is in flight, incoming alerts are skipped with reason global_in_flight."""
    data = load_fixture("alertmanager_firing.json")
    alert = AlertmanagerWebhook.model_validate(data).alerts[0]

    # Set in flight
    rails.set_in_flight(True)

    decision = rails.evaluate(alert, mock_k8s)
    assert decision.allowed is False
    assert decision.decision == "skip"
    assert decision.reason == "global_in_flight"

    # Key check: skipped decision MUST NOT mark incident as handled
    key = (alert.alertname, alert.service, alert.startsAt)
    assert key not in rails.active_incidents or rails.active_incidents[key].attempts == 0

    # Release in flight
    rails.set_in_flight(False)
    decision2 = rails.evaluate(alert, mock_k8s)
    assert decision2.allowed is True
    assert decision2.decision == "act"


def test_dry_run_performs_zero_k8s_writes(mock_k8s):
    """DRY_RUN mode performs no mutating calls against Kubernetes APIs."""
    alert_api = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    alert_pod = Alert(
        status="firing",
        labels={"alertname": "PodCrashLooping", "service": "api", "pod": "api-1", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    rollout_action = RolloutRestartAction()
    res1 = rollout_action.execute(alert_api, mock_k8s, dry_run=True)
    assert res1 == "dry_run"
    mock_k8s.apps_v1.patch_namespaced_deployment.assert_not_called()

    pod_action = RestartPodAction()
    res2 = pod_action.execute(alert_pod, mock_k8s, dry_run=True)
    assert res2 == "dry_run"
    mock_k8s.core_v1.delete_namespaced_pod.assert_not_called()


def test_missing_or_foreign_remediation_label_refused(rails, mock_k8s):
    """Alerts without remediation='auto' are refused."""
    data = load_fixture("alertmanager_firing.json")
    data["alerts"][0]["labels"]["remediation"] = "manual"
    alert = AlertmanagerWebhook.model_validate(data).alerts[0]

    d = rails.evaluate(alert, mock_k8s)
    assert d.allowed is False
    assert d.decision == "refuse"
    assert d.reason == "not_auto_remediation"


def test_missing_or_foreign_namespace_refused(rails, mock_k8s):
    """Alerts for non-platform namespaces are refused."""
    data = load_fixture("alertmanager_firing.json")
    data["alerts"][0]["labels"]["namespace"] = "kube-system"
    alert = AlertmanagerWebhook.model_validate(data).alerts[0]

    d = rails.evaluate(alert, mock_k8s)
    assert d.allowed is False
    assert d.decision == "refuse"
    assert d.reason == "not_platform_namespace"


def test_malformed_body_returns_400():
    """Malformed webhook request body returns HTTP 400 Bad Request."""
    client = TestClient(app)
    resp = client.post("/webhook", content="not-a-valid-json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 400


def test_pod_owner_not_allowlisted_deployment_refused(rails, mock_k8s):
    """A pod whose owner is not an allowlisted Deployment is refused."""
    mock_pod = MagicMock()
    mock_pod.metadata.owner_references = [make_owner_ref(kind="ReplicaSet", name="fake-rs")]
    mock_rs = MagicMock()
    mock_rs.metadata.owner_references = [make_owner_ref(kind="Deployment", name="unauthorized-app")]
    mock_k8s.core_v1.read_namespaced_pod.return_value = mock_pod
    mock_k8s.apps_v1.read_namespaced_replica_set.return_value = mock_rs

    alert = Alert(
        status="firing",
        labels={"alertname": "PodCrashLooping", "service": "api", "pod": "spoofed-pod", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    d = rails.evaluate(alert, mock_k8s)
    assert d.allowed is False
    assert d.decision == "refuse"
    assert d.reason == "owner_deployment_mismatch"


def test_unknown_alertname_ignored(rails, mock_k8s):
    """Alerts with unmapped alertnames are ignored."""
    alert = Alert(
        status="firing",
        labels={"alertname": "DiskPressure", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    d = rails.evaluate(alert, mock_k8s)
    assert d.allowed is False
    assert d.decision == "ignore"
    assert d.reason == "unknown_alert"


def test_healthz_and_readyz_and_metrics():
    """Verify healthz, readyz, and metrics endpoints return 200."""
    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert "controller_alerts_received_total" in metrics_resp.text
