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
- **Top-Level Key Order**:
  `["receiver","status","alerts","notification_reason","groupLabels","commonLabels","commonAnnotations","routeLabels","externalURL","version","groupKey","truncatedAlerts"]`

## Extraction Mechanism

All fixtures were captured by saving raw logs from `deploy/echo-receiver` to `/tmp/echo_raw.log`:
```bash
kubectl -n platform logs deploy/echo-receiver > /tmp/echo_raw.log
```
Each payload was extracted by matching alertname, status, and fingerprint, and copied byte-for-byte to `controller/tests/fixtures/`.

## Fixture Catalog & Checksums

| File | Status | Alertname | Fingerprint(s) | SHA256 Checksum | Extraction Command |
|---|---|---|---|---|---|
| `alertmanager_firing.json` | firing | `ApiHighErrorRate` | `61652fcbcf60c7a2` | `e25b139b2d8a29b667734c6d822f8ddd16b5f99e85cf3cd466448518d8f2ab87` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="firing" and .alerts[0].labels.alertname=="ApiHighErrorRate" and .alerts[0].fingerprint=="61652fcbcf60c7a2")'` |
| `alertmanager_resolved.json` | resolved | `ApiHighErrorRate` | `61652fcbcf60c7a2` | `8240d029154e7d647ad26b60b70144830fed739b52c325615eb9027bba9a3790` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="resolved" and .alerts[0].labels.alertname=="ApiHighErrorRate" and .alerts[0].fingerprint=="61652fcbcf60c7a2")'` |
| `alertmanager_worker_backlog.json` | firing | `WorkerQueueBacklog` | `220226a30f6bcdf1` | `eaf2802c29929639ce62057a40a9d507b9f76214ef3db3a11ae734cfd36efeed` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq -s 'map(select(.status=="firing" and .alerts[0].labels.alertname=="WorkerQueueBacklog" and .alerts[0].fingerprint=="220226a30f6bcdf1"))[0]'` |
| `alertmanager_api_high_latency.json` | firing | `ApiHighLatency` | `70739fcfbdd178ab` | `bfe805a6d5cc4d6617f1ae4b8fe9b6a8b24b79dbbe3b819d86a2d2d9cb848698` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="firing" and .alerts[0].labels.alertname=="ApiHighLatency" and .alerts[0].fingerprint=="70739fcfbdd178ab")'` |
| `alertmanager_pod_restarted.json` | firing | `PodRestarted` | `174436af7f633789` | `64d05134a70f44569c7bc8ffa789ea4d9004ecd97d5f3fcf6ef75b4f1417a0da` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="firing" and (.alerts\|length)==1 and .alerts[0].labels.alertname=="PodRestarted" and .alerts[0].fingerprint=="174436af7f633789")'` |
| `alertmanager_multi_alert.json` | firing | `PodRestarted` (2 alerts) | `e9cd3b53f8f69824`, `174436af7f633789` | `1a4b003ef5e1a6dd69ec657365fe0c6002778080b8acc5c77e336324c820ec6e` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="firing" and (.alerts\|length)==2 and .alerts[0].labels.alertname=="PodRestarted")'` |
| `alertmanager_pod_crashlooping.json` | firing | `PodCrashLooping` | `0dfa0bd1435cf4ab` | `32f338460be93da3ea9f5e09192bf5597ccfdcba1318adb0622eebcdfd433ab9` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="firing" and .alerts[0].labels.alertname=="PodCrashLooping" and .alerts[0].fingerprint=="0dfa0bd1435cf4ab")'` |
| `alertmanager_pod_crashlooping_resolved.json` | resolved | `PodCrashLooping` | `0dfa0bd1435cf4ab` | `8816176ff87fb68eb4e2c2c8cf4439efc1d55658699a582c63ae4a96a3ccea4e` | `grep '^{"receiver"' /tmp/echo_raw.log \| jq 'select(.status=="resolved" and .alerts[0].labels.alertname=="PodCrashLooping" and .alerts[0].fingerprint=="0dfa0bd1435cf4ab")'` |
