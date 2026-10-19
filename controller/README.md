# Self-Healing Controller (v1)

Automated remediation controller that receives Alertmanager v4 webhooks, evaluates safety rails, and initiates self-healing cluster actions.

## Architecture

- **Framework**: Python 3.12, FastAPI, official Kubernetes client.
- **Port**: 8080 (container and Service port, named `http`).
- **Endpoints**:
  - `POST /webhook`: Alertmanager v4 webhook receiver (asynchronous dispatch, fast 200 response).
  - `GET /healthz`: Liveness probe (no external K8s API dependencies).
  - `GET /readyz`: Readiness probe.
  - `GET /metrics`: Prometheus metrics scrape endpoint.
- **Hardening**: Multi-stage Docker build, non-root user (`appuser` UID 10001), `readOnlyRootFilesystem: true`, scoped namespaced RBAC.

## Safety Rails & Policy

1. **Namespace & Remediation Filters**: Strictly drops alerts where `namespace != "platform"` or `labels.remediation != "auto"`.
2. **Service Allowlist**: Only permits actions against allowlisted Deployments (`auth`, `api`, `worker`, `frontend`). Infrastructure components (`redis`, `controller`, `echo-receiver`) are rejected.
3. **OwnerReferences Verification (Anti-Spoofing)**: For pod-level deletions (`restart_pod`), the controller traverses `.metadata.ownerReferences` (`Pod -> ReplicaSet -> Deployment`) to verify that the pod actually belongs to an allowlisted Deployment before deleting it.
4. **Idempotency & Incident Identity**: Keyed by `(alertname, service, startsAt)`. Active alerts repeating with identical `startsAt` do not create duplicate actions.
5. **Cooldown (240s)**: Cooldown window keyed on `(alertname, service)` gives the system time to stabilize and Prometheus to clear the alert. A second crash-looping pod of the same service inside cooldown is skipped.
6. **Circuit Breaker (Max 3 actions / service / hour)**: Bounded sliding window. If an incident requires a 4th action within the hour, the controller transitions to `escalate` (`needs_human: True`, logs an `ERROR` once), refusing further actions to prevent restart loops.
7. **Global In-Flight Cap**: At most one remediation action executes at any given time across the entire cluster to prevent thundering herd restarts. Incoming alerts during in-flight execution are skipped with reason `global_in_flight` without being marked as handled.
8. **DRY_RUN Mode**: Controlled by `DRY_RUN` environment variable (default: `"true"`). Simulates all decisions, ownership verifications, and incident logs with zero mutating Kubernetes calls.

## v1 Limitations

1. **In-Memory State**: Cooldowns, circuit breaker counters, open incidents, and in-flight tracking live in process memory and are reset if the controller pod restarts.
2. **Cluster-Internal Unauthenticated Webhook**: The `/webhook` endpoint is unauthenticated within the cluster network. This risk is mitigated by strict namespace/remediation label filtering, allowlisting, Kubernetes API `ownerReferences` verification, and rate limits.
3. **Restart Scope**: Controller v1 only executes container or deployment restarts. Automated restarts cannot resolve bad image releases or code bugs; Stage 7 adds automated GitOps rollbacks and scale-ups.
4. **Demo-Tuned Thresholds**: Cooldowns (240s) and circuit breaker caps (3 actions/hr) are configured for fast feedback during demonstrations.
