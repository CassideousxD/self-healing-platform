"""Pydantic schemas for Alertmanager v4 webhooks."""

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class Alert(BaseModel):
    """An individual alert within an Alertmanager webhook payload."""

    model_config = ConfigDict(extra="ignore")

    status: str
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: str
    endsAt: str = "0001-01-01T00:00:00Z"
    generatorURL: Optional[str] = ""
    fingerprint: Optional[str] = ""

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "")

    @property
    def namespace(self) -> str:
        return self.labels.get("namespace", "")

    @property
    def service(self) -> str:
        return self.labels.get("service", "")

    @property
    def pod(self) -> str:
        return self.labels.get("pod", "")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "")

    @property
    def remediation(self) -> str:
        return self.labels.get("remediation", "")


class AlertmanagerWebhook(BaseModel):
    """Alertmanager v4 webhook request body."""

    model_config = ConfigDict(extra="ignore")

    receiver: str
    status: str
    alerts: List[Alert] = Field(default_factory=list)
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = ""
    version: Optional[str] = "4"
    groupKey: Optional[str] = ""
    truncatedAlerts: Optional[int] = 0
