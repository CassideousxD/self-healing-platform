"""Structured incident logger outputting JSON lines to stdout and disk."""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_LOG_PATH = os.getenv("INCIDENT_LOG_PATH", "/var/lib/controller/incidents.jsonl")


def log_incident_event(
    event: str,
    incident_id: str,
    alertname: str,
    service: str,
    pod: Optional[str] = None,
    severity: Optional[str] = None,
    fingerprint: Optional[str] = None,
    starts_at: Optional[str] = None,
    decision: Optional[str] = None,
    reason: Optional[str] = None,
    action: Optional[str] = None,
    outcome: Optional[str] = None,
    duration_ms: Optional[float] = None,
    dry_run: bool = True,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Emit a structured incident event line to stdout and the local jsonl log file."""
    record: Dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "incident_id": incident_id,
        "alertname": alertname,
        "service": service,
        "pod": pod or "",
        "severity": severity or "",
        "fingerprint": fingerprint or "",
        "starts_at": starts_at or "",
        "decision": decision or "",
        "reason": reason or "",
        "action": action or "",
        "outcome": outcome or "",
        "duration_ms": duration_ms if duration_ms is not None else 0.0,
        "dry_run": dry_run,
    }
    if extra:
        record.update(extra)

    line = json.dumps(record)
    # Output to stdout for Alloy -> Loki scraping
    sys.stdout.write(line + "\n")
    sys.stdout.flush()

    # Append to local jsonl file for persistence / Stage 7 postmortems
    log_file_path = os.getenv("INCIDENT_LOG_PATH", DEFAULT_LOG_PATH)
    try:
        p = Path(log_file_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except (OSError, PermissionError) as e:
        sys.stderr.write(
            f"Warning: could not write incident log to {log_file_path}: {e}. Falling back to stdout only.\n"
        )
        sys.stderr.flush()

    return record
