import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger("controller.policy")

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
        self.is_valid: bool = False
        self.load_error: Optional[str] = None
        self.policy: Dict[str, Any] = {}
        self.load_policy()

    def load_policy(self) -> None:
        """Load policy from YAML file, failing closed if missing or unparseable."""
        p = Path(self.policy_path)
        if not p.is_file():
            self.is_valid = False
            self.load_error = f"Policy file not found: {self.policy_path}"
            logger.error(f"PolicyEngine initialization failed: {self.load_error}")
            return

        try:
            with open(p, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f)
        except Exception as e:
            self.is_valid = False
            self.load_error = f"Failed to parse policy YAML from {self.policy_path}: {e}"
            logger.error(f"PolicyEngine initialization failed: {self.load_error}")
            return

        if not isinstance(loaded, dict):
            self.is_valid = False
            self.load_error = f"Policy file {self.policy_path} is not a valid YAML mapping"
            logger.error(f"PolicyEngine initialization failed: {self.load_error}")
            return

        merged = dict(DEFAULT_POLICY)
        merged.update(loaded)
        self.policy = merged
        self.is_valid = True
        self.load_error = None

    @property
    def allowlist(self) -> List[str]:
        if not self.is_valid:
            return []
        return self.policy.get("allowlist", DEFAULT_POLICY["allowlist"])

    @property
    def cooldown_seconds(self) -> int:
        if not self.is_valid:
            return 240
        return int(self.policy.get("cooldown_seconds", DEFAULT_POLICY["cooldown_seconds"]))

    @property
    def max_actions_per_hour(self) -> int:
        if not self.is_valid:
            return 3
        cb = self.policy.get("circuit_breaker", {})
        return int(cb.get("max_actions_per_hour", 3))

    def get_action_for_alert(self, alertname: str) -> Optional[str]:
        """Look up the configured action for an alertname."""
        if not self.is_valid:
            return None
        rules = self.policy.get("rules", {})
        return rules.get(alertname)

    def is_service_allowed(self, service: str) -> bool:
        """Check whether a service is in the remediation allowlist."""
        if not self.is_valid:
            return False
        return service in self.allowlist
