"""Unit tests for the self-healing controller, safety rails, policies, and actions."""

import io
import json
import logging
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient
from kubernetes.client.rest import ApiException

from controller.app import main
from controller.app.actions import RestartPodAction, RolloutRestartAction
from controller.app.incident_log import log_incident_event
from controller.app.main import _process_alert_sync, app
from controller.app.policy import DEFAULT_POLICY, PolicyEngine
from controller.app.rails import SafetyRails
from controller.app.schemas import Alert, AlertmanagerWebhook

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def policy(tmp_path):
    """Create a policy engine with clean default test rules from a valid temporary file."""
    p_file = tmp_path / "policy.yaml"
    p_file.write_text(yaml.dump(DEFAULT_POLICY), encoding="utf-8")
    return PolicyEngine(policy_path=str(p_file))


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


def make_owner_ref(kind: str, name: str) -> MagicMock:
    """Helper to create a Kubernetes OwnerReference mock with explicit attribute assignment."""
    ref = MagicMock()
    ref.kind = kind
    ref.name = name
    return ref


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
    # Action releases in-flight flag after execution
    rails.release_in_flight(d1.in_flight_token)

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
    rails.release_in_flight(d1.in_flight_token)

    # Simulate passage of 250s (greater than 240s cooldown)
    with patch("time.time", return_value=time.time() + 250):
        d2 = rails.evaluate(alert, mock_k8s)
        assert d2.allowed is True
        assert d2.decision == "act"
        assert d2.attempt == 2


def test_cooldown_checked_before_circuit_breaker(rails, mock_k8s):
    """Order of checks: cooldown checked first. A delivery 5s after the 3rd action is cooldown_active, not escalate."""
    data = load_fixture("alertmanager_firing.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    base_time = 10000.0

    # Action 1 (t = 10000)
    with patch("time.time", return_value=base_time):
        d1 = rails.evaluate(alert, mock_k8s)
        assert d1.allowed is True
        assert d1.attempt == 1
        rails.release_in_flight(d1.in_flight_token)

    # Action 2 (t = 10300, cooldown 240s elapsed)
    with patch("time.time", return_value=base_time + 300):
        d2 = rails.evaluate(alert, mock_k8s)
        assert d2.allowed is True
        assert d2.attempt == 2
        rails.release_in_flight(d2.in_flight_token)

    # Action 3 (t = 10600)
    with patch("time.time", return_value=base_time + 600):
        d3 = rails.evaluate(alert, mock_k8s)
        assert d3.allowed is True
        assert d3.attempt == 3
        rails.release_in_flight(d3.in_flight_token)

    # Delivery 5 seconds after 3rd action (t = 10605):
    # Must be skip(cooldown_active), NOT escalate!
    with patch("time.time", return_value=base_time + 605):
        d_cooldown = rails.evaluate(alert, mock_k8s)
        assert d_cooldown.allowed is False
        assert d_cooldown.decision == "skip"
        assert d_cooldown.reason == "cooldown_active"

    # Delivery after cooldown has elapsed (t = 10900, +300s):
    # Now breaker applies: 3 actions in 1 hour -> 4th attempt trips circuit breaker and escalates once
    with patch("time.time", return_value=base_time + 900):
        d_escalate = rails.evaluate(alert, mock_k8s)
        assert d_escalate.allowed is False
        assert d_escalate.decision == "escalate"
        assert d_escalate.reason == "circuit_breaker_tripped"
        assert d_escalate.needs_human is True

    # Subsequent delivery after cooldown (t = 11200):
    # Already escalated -> skipped without log spam
    with patch("time.time", return_value=base_time + 1200):
        d_already = rails.evaluate(alert, mock_k8s)
        assert d_already.allowed is False
        assert d_already.decision == "skip"
        assert d_already.reason == "already_escalated"


def test_circuit_breaker_sliding_window(rails, mock_k8s):
    """Actions older than 1 hour (3600s) slide out of the circuit breaker window and stop counting."""
    data = load_fixture("alertmanager_firing.json")
    webhook = AlertmanagerWebhook.model_validate(data)
    alert = webhook.alerts[0]

    base_time = 20000.0

    # Execute 3 actions
    for offset in [0, 300, 600]:
        with patch("time.time", return_value=base_time + offset):
            d = rails.evaluate(alert, mock_k8s)
            assert d.allowed is True
            rails.release_in_flight(d.in_flight_token)

    # Advance time by 3605s (actions from t=0 and t=300 have slid out of the 1-hr window)
    with patch("time.time", return_value=base_time + 3605):
        d_new = rails.evaluate(alert, mock_k8s)
        assert d_new.allowed is True
        assert d_new.decision == "act"
        assert d_new.attempt == 4


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


def test_restart_pod_no_k8s_client_refused_zero_writes(rails):
    """FAIL CLOSED: restart_pod without a valid Kubernetes client is refused and executes zero writes."""
    alert = Alert(
        status="firing",
        labels={"alertname": "PodCrashLooping", "service": "api", "pod": "api-6fb546cd54-abcde", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    # Pass k8s_api=None
    d1 = rails.evaluate(alert, k8s_api=None)
    assert d1.allowed is False
    assert d1.decision == "refuse"
    assert d1.reason == "k8s_client_unavailable"

    # Pass client with None sub-apis
    empty_client = MagicMock(core_v1=None, apps_v1=None)
    d2 = rails.evaluate(alert, k8s_api=empty_client)
    assert d2.allowed is False
    assert d2.decision == "refuse"
    assert d2.reason == "k8s_client_unavailable"


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
    rails.release_in_flight(d1.in_flight_token)

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
    rails.release_in_flight(d_fire.in_flight_token)

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

    # Set in flight manually with a token
    rails.global_in_flight = True
    rails.in_flight_since = time.time()
    rails._in_flight_token = "manual_token"

    decision = rails.evaluate(alert, mock_k8s)
    assert decision.allowed is False
    assert decision.decision == "skip"
    assert decision.reason == "global_in_flight"

    # Key check: skipped decision MUST NOT mark incident as handled
    key = (alert.alertname, alert.service, alert.startsAt)
    assert key not in rails.active_incidents or rails.active_incidents[key].attempts == 0

    # Release in flight with token
    rails.release_in_flight("manual_token")
    decision2 = rails.evaluate(alert, mock_k8s)
    assert decision2.allowed is True
    assert decision2.decision == "act"
    rails.release_in_flight(decision2.in_flight_token)


def test_concurrent_evaluate_calls_global_in_flight(rails, mock_k8s):
    """Two concurrent threads calling evaluate() at the same moment: exactly one is approved."""
    alert1 = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    alert2 = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "auth", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    results = []

    def run_eval(alert):
        res = rails.evaluate(alert, mock_k8s)
        results.append(res)

    t1 = threading.Thread(target=run_eval, args=(alert1,))
    t2 = threading.Thread(target=run_eval, args=(alert2,))

    t1.start()
    t2.start()
    t1.join()
    t2.join()

    decisions = [r.decision for r in results]
    assert "act" in decisions
    assert "skip" in decisions
    skip_decision = next(r for r in results if r.decision == "skip")
    assert skip_decision.reason == "global_in_flight"
    act_decision = next(r for r in results if r.decision == "act")
    rails.release_in_flight(act_decision.in_flight_token)


def test_in_flight_flag_released_on_action_exception(rails, mock_k8s):
    """When action execution raises an exception, the in-flight flag is safely released in finally and worker continues."""
    alert = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    with patch("controller.app.actions.RolloutRestartAction.execute", side_effect=RuntimeError("K8s API boom")):
        _process_alert_sync(alert, rails, mock_k8s)

    # In-flight cap must be cleanly released despite exception
    assert rails.global_in_flight is False
    assert rails._in_flight_token is None


def test_stale_in_flight_flag_auto_expires(rails, mock_k8s):
    """A stale in-flight flag older than 180s auto-expires, logs an error, and permits the next action."""
    alert = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    rails.global_in_flight = True
    rails.in_flight_since = time.time() - 190.0  # 190s ago (stale)
    rails._in_flight_token = "stale_token"

    decision = rails.evaluate(alert, mock_k8s)
    assert decision.allowed is True
    assert decision.decision == "act"
    assert rails.global_in_flight is True
    assert rails._in_flight_token == decision.in_flight_token
    rails.release_in_flight(decision.in_flight_token)


def test_mutating_in_flight_log_only_processed_flag_still_held(rails, mock_k8s):
    """A mutating action is in flight; a log_only alert is processed to completion through

    _process_alert_sync; the flag is STILL held and a second mutating alert is still skip(global_in_flight).
    """
    mutating_alert1 = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    d1 = rails.evaluate(mutating_alert1, mock_k8s)
    assert d1.allowed is True
    assert rails.global_in_flight is True
    assert rails._in_flight_token == d1.in_flight_token
    initial_token = d1.in_flight_token

    # Process a log_only alert to completion
    log_only_alert = Alert(
        status="firing",
        labels={"alertname": "PodRestarted", "service": "api", "pod": "api-1", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:01:00Z",
    )
    _process_alert_sync(log_only_alert, rails, mock_k8s)

    # Flag MUST STILL be held with the original mutating token!
    assert rails.global_in_flight is True
    assert rails._in_flight_token == initial_token

    # A second mutating alert is evaluated: must be skip(global_in_flight)
    mutating_alert2 = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "auth", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:02:00Z",
    )
    d2 = rails.evaluate(mutating_alert2, mock_k8s)
    assert d2.allowed is False
    assert d2.decision == "skip"
    assert d2.reason == "global_in_flight"

    # Finally release original token
    rails.release_in_flight(initial_token)
    assert rails.global_in_flight is False
    assert rails._in_flight_token is None


def test_action_watchdog_expiry_token_safety(rails, mock_k8s):
    """Action A takes the flag, the watchdog expires it, action B is approved with a new token,

    A then finishes: B's flag is still held; B's own release clears it.
    """
    base_time = 10000.0
    alert_a = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    alert_b = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "auth", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:01:00Z",
    )

    with patch("time.time", return_value=base_time):
        d_a = rails.evaluate(alert_a, mock_k8s)
        assert d_a.allowed is True
        token_a = d_a.in_flight_token
        assert rails._in_flight_token == token_a

    # 200s later (watchdog > 180s expired): Action B is evaluated
    with patch("time.time", return_value=base_time + 200.0):
        d_b = rails.evaluate(alert_b, mock_k8s)
        assert d_b.allowed is True
        token_b = d_b.in_flight_token
        assert token_b != token_a
        assert rails._in_flight_token == token_b
        assert rails.global_in_flight is True

        # Now Action A finishes and attempts to release token_a (stale)
        rails.release_in_flight(token_a)

        # Token B's flag MUST STILL be held!
        assert rails.global_in_flight is True
        assert rails._in_flight_token == token_b

        # Action B finishes and releases token_b
        rails.release_in_flight(token_b)
        assert rails.global_in_flight is False
        assert rails._in_flight_token is None


def test_log_incident_event_decision_error_releases_flag(rails, mock_k8s):
    """log_incident_event raises once on the 'decision' event right after evaluate()

    approved a mutating action: the flag is released and no exception escapes the worker.
    """
    alert = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    orig_log = log_incident_event

    def mock_log(**kwargs):
        if kwargs.get("event") == "decision":
            raise RuntimeError("Disk full or log failure on decision")
        return orig_log(**kwargs)

    with patch("controller.app.main.log_incident_event", side_effect=mock_log):
        # Must not raise
        _process_alert_sync(alert, rails, mock_k8s)

    # In-flight flag must be released in finally block
    assert rails.global_in_flight is False
    assert rails._in_flight_token is None


def test_unexpected_exception_in_evaluate_logged_as_error_event(rails, mock_k8s):
    """An unexpected exception inside evaluate() is logged as an 'error' event and does not crash the worker or leave flag set."""
    alert = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    logged_events = []

    def spy_log(**kwargs):
        logged_events.append(kwargs)
        return {"event": kwargs.get("event")}

    with patch.object(rails, "evaluate", side_effect=RuntimeError("Unexpected rails crash")), \
         patch("controller.app.main.log_incident_event", side_effect=spy_log):
        # Must not raise
        _process_alert_sync(alert, rails, mock_k8s)

    assert rails.global_in_flight is False
    error_event = next((e for e in logged_events if e.get("event") == "error"), None)
    assert error_event is not None
    assert "Unexpected rails crash" in error_event.get("extra", {}).get("error", "")


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


def test_healthz_and_readyz_and_metrics(tmp_path, monkeypatch):
    """Verify healthz, readyz, and metrics endpoints return 200 when policy is valid."""
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text(yaml.dump(DEFAULT_POLICY), encoding="utf-8")
    valid_pe = PolicyEngine(policy_path=str(policy_file))
    monkeypatch.setattr(main, "policy_engine", valid_pe)

    client = TestClient(app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200

    metrics_resp = client.get("/metrics")
    assert metrics_resp.status_code == 200
    assert "controller_alerts_received_total" in metrics_resp.text


def test_dry_run_approved_restart_releases_in_flight_zero_writes(rails, mock_k8s, monkeypatch):
    """With DRY_RUN=true, process an approved restart alert through _process_alert_sync:

    asserts global_in_flight is False afterwards, and zero Kubernetes mutating calls are made.
    """
    monkeypatch.setenv("DRY_RUN", "true")
    alert = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )

    # Process alert through _process_alert_sync
    _process_alert_sync(alert, rails, mock_k8s)

    # In-flight flag must be cleanly released to False
    assert rails.global_in_flight is False

    # Zero Kubernetes mutating calls made
    mock_k8s.apps_v1.patch_namespaced_deployment.assert_not_called()
    mock_k8s.core_v1.delete_namespaced_pod.assert_not_called()


def test_missing_or_unparseable_policy_fails_closed_refuses_actions_and_readyz_503(tmp_path, monkeypatch, caplog):
    """Missing or unparseable policy fails closed: refuses actions, logs ERROR, readyz returns 503."""
    # 1. Missing policy file
    missing_path = tmp_path / "nonexistent_policy.yaml"
    with caplog.at_level(logging.ERROR):
        pe_missing = PolicyEngine(policy_path=str(missing_path))
    assert pe_missing.is_valid is False
    assert "Policy file not found" in pe_missing.load_error
    assert any("PolicyEngine initialization failed" in r.message for r in caplog.records)

    rails_missing = SafetyRails(pe_missing)
    alert = Alert(
        status="firing",
        labels={"alertname": "ApiHighErrorRate", "service": "api", "remediation": "auto", "namespace": "platform"},
        startsAt="2026-10-18T12:00:00Z",
    )
    decision = rails_missing.evaluate(alert)
    assert decision.allowed is False
    assert decision.decision == "refuse"
    assert decision.reason == "policy_invalid_or_missing"

    # Readyz with missing policy returns 503
    monkeypatch.setattr(main, "policy_engine", pe_missing)
    client = TestClient(app)
    ready_missing = client.get("/readyz")
    assert ready_missing.status_code == 503
    assert ready_missing.json()["status"] == "unhealthy"

    # 2. Unparseable / corrupted policy file
    corrupt_path = tmp_path / "corrupt_policy.yaml"
    corrupt_path.write_text(":: invalid: yaml: { [", encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        pe_corrupt = PolicyEngine(policy_path=str(corrupt_path))
    assert pe_corrupt.is_valid is False
    assert "Failed to parse policy YAML" in pe_corrupt.load_error

    rails_corrupt = SafetyRails(pe_corrupt)
    decision_corrupt = rails_corrupt.evaluate(alert)
    assert decision_corrupt.allowed is False
    assert decision_corrupt.decision == "refuse"
    assert decision_corrupt.reason == "policy_invalid_or_missing"

    monkeypatch.setattr(main, "policy_engine", pe_corrupt)
    ready_corrupt = client.get("/readyz")
    assert ready_corrupt.status_code == 503
    assert ready_corrupt.json()["status"] == "unhealthy"


def test_incident_log_survives_unwritable_directory(monkeypatch):
    """Incident logger writes to stdout and logs a warning on stderr when the disk directory is unwritable."""
    monkeypatch.setenv("INCIDENT_LOG_PATH", "/unwritable_root_directory/incidents.jsonl")

    stderr_buf = io.StringIO()
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_buf)
    monkeypatch.setattr(sys, "stdout", stdout_buf)

    record = log_incident_event(
        event="alert_received",
        incident_id="test-incident",
        alertname="ApiHighErrorRate",
        service="api",
    )

    assert record["event"] == "alert_received"
    stdout_val = stdout_buf.getvalue()
    assert '"event": "alert_received"' in stdout_val
    stderr_val = stderr_buf.getvalue()
    assert "Warning: could not write incident log to" in stderr_val


def test_real_deploy_base_policy_file_matches_schema_and_code():
    """Verify that deploy/base/policy.yaml adheres to PolicyEngine schema and contains expected production rules."""
    real_policy_file = Path(__file__).resolve().parents[2] / "deploy" / "base" / "policy.yaml"
    assert real_policy_file.is_file(), f"Policy file not found at {real_policy_file}"

    pe = PolicyEngine(policy_path=str(real_policy_file))
    assert pe.is_valid is True
    assert pe.load_error is None

    assert pe.allowlist == ["auth", "api", "worker", "frontend"]
    assert pe.cooldown_seconds == 240
    assert pe.max_actions_per_hour == 3

    assert pe.get_action_for_alert("PodRestarted") == "log_only"
    assert pe.get_action_for_alert("PodCrashLooping") == "restart_pod"
    assert pe.get_action_for_alert("ApiHighErrorRate") == "rollout_restart"
    assert pe.get_action_for_alert("ApiHighLatency") == "log_only"
    assert pe.get_action_for_alert("WorkerQueueBacklog") == "log_only"

