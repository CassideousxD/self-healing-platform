# Alertmanager Webhook Fixtures

All fixtures in this directory are **verbatim captures** received by the `deploy/echo-receiver` webhook receiver from Alertmanager running on the local Kubernetes cluster. No payload has been hand-edited, synthesized, or reconstructed.

## Alertmanager Environment

- **Image**: `quay.io/prometheus/alertmanager:v0.34.1`
- **Full Version Info**:
  ```
  alertmanager, version 0.34.1 (branch: HEAD, revision: 73c6bfe7393929211294c1954f30d8ed78e4d0ad)
    build user:       root@d3b71085e163
    build date:       20260917-07:45:14
    go version:       go1.26.8
    platform:         linux/arm64
  ```
- **Schema Note**: In Alertmanager v0.34+, standard v4 webhook payloads include `routeLabels` and `notification_reason` (e.g. `"first notification"` when an alert group starts firing, or `"all alerts resolved"` when all alerts in a group resolve).

## Fixtures Catalog

| File | Status | Alert Name | Service | Captured Timestamp (UTC) | Trigger Mechanism |
|---|---|---|---|---|---|
| `alertmanager_firing.json` | firing | `ApiHighErrorRate` | `api` | 2026-10-18T17:26:53Z | POST `/chaos/errors?rate=1.0` injected on both API pods under traffic from `scripts/load.sh`. |
| `alertmanager_resolved.json` | resolved | `ApiHighErrorRate` | `api` | 2026-10-18T17:27:38Z | POST `/chaos/reset` injected on both API pods; 5xx error rate dropped to 0. |
| `alertmanager_worker_backlog.json` | firing | `WorkerQueueBacklog` | `worker` | 2026-10-18T17:28:38Z | `kubectl -n platform scale deploy/worker --replicas=0` while generating jobs via API `/jobs` endpoint. |
| `alertmanager_api_high_latency.json` | firing | `ApiHighLatency` | `api` | 2026-10-18T17:29:08Z | POST `/chaos/latency?ms=350` injected on both API pods under traffic from `scripts/load.sh`. |
| `alertmanager_pod_restarted.json` | firing | `PodRestarted` | `api` | 2026-10-18T17:30:53Z | POST `/chaos/crash` on single API pod `api-576f78c799-wwsxr`. |
| `alertmanager_multi_alert.json` | firing | `PodRestarted` (2 alerts) | `api` | 2026-10-18T17:44:08Z | Concurrent POST `/chaos/crash` sent to both `api` pods (`api-576f78c799-tswkt` and `api-576f78c799-wwsxr`) within 1 second. Alertmanager grouped both into one notification. |
| `alertmanager_pod_crashlooping.json` | firing | `PodCrashLooping` | `crashtest` | 2026-10-18T17:47:53Z | Created `crashtest` deployment (`busybox:1.36 -- sh -c "exit 1"`). Fired after `1m` in `CrashLoopBackOff`. Proves regex on deployment-style pod names. |
| `alertmanager_pod_crashlooping_resolved.json` | resolved | `PodCrashLooping` | `crashtest` | 2026-10-18T17:48:23Z | Deleted `crashtest` deployment; metric cleared and alert resolved. |
