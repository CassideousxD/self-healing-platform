"""Prometheus metrics for the self-healing controller."""

from prometheus_client import Counter, Gauge

ALERTS_RECEIVED_TOTAL = Counter(
    "controller_alerts_received_total",
    "Total Alertmanager alerts received",
    ["alertname", "status"],
)

DECISIONS_TOTAL = Counter(
    "controller_decisions_total",
    "Total remediation decisions made by the policy engine",
    ["decision"],
)

ACTIONS_TOTAL = Counter(
    "controller_actions_total",
    "Total remediation actions executed",
    ["action", "outcome"],
)

INCIDENTS_OPEN = Gauge(
    "controller_incidents_open",
    "Current number of open/active incidents",
)
