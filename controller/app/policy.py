"""Policy engine configuration and rule evaluation."""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

DEFAULT_POLICY_PATH = os.getenv("POLICY_PATH", "/etc/controller/policy.yaml")

DEFAULT_POLICY: Dict[str, Any] = {
    "allowlist": ["auth", "api", "worker", "frontend"],
    "cooldown_seconds": 240,
    "circuit_breaker": {
        "max_actions_per_hour": 3,
    },
    "rules": {
        "PodRestarted": "log_only",
        "PodCrashLooping": "restart_pod",
        "ApiHighErrorRate": "rollout_restart",
        "ApiHighLatency": "log_only",
        "WorkerQueueBacklog": "log_only",
    },
}


class PolicyEngine:
    """Manages policy loading, service allowlists, and alert action mappings."""

    def __init__(self, policy_path: Optional[str] = None):
        self.policy_path = policy_path or DEFAULT_POLICY_PATH
        self.policy: Dict[str, Any] = self.load_policy()

    def load_policy(self) -> Dict[str, Any]:
        """Load policy from YAML file, falling back to compiled defaults."""
        p = Path(self.policy_path)
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    loaded = yaml.safe_load(f)
                    if isinstance(loaded, dict):
                        merged = dict(DEFAULT_POLICY)
                        merged.update(loaded)
                        return merged
            except Exception:
                pass
        return dict(DEFAULT_POLICY)

    @property
    def allowlist(self) -> List[str]:
        return self.policy.get("allowlist", DEFAULT_POLICY["allowlist"])

    @property
    def cooldown_seconds(self) -> int:
        return int(self.policy.get("cooldown_seconds", DEFAULT_POLICY["cooldown_seconds"]))

    @property
    def max_actions_per_hour(self) -> int:
        cb = self.policy.get("circuit_breaker", {})
        return int(cb.get("max_actions_per_hour", 3))

    def get_action_for_alert(self, alertname: str) -> Optional[str]:
        """Look up the configured action for an alertname."""
        rules = self.policy.get("rules", {})
        return rules.get(alertname)

    def is_service_allowed(self, service: str) -> bool:
        """Check whether a service is in the remediation allowlist."""
        return service in self.allowlist
