# Self-Healing Microservices Deployment Pipeline

An enterprise-grade, GitOps-driven self-healing microservices platform deployed on Kubernetes with automated remediation, full-stack observability, and declarative configuration.

---

## 🏛 Architecture Overview

```
                      +-------------------+
                      |   GitHub / Git    | (Single Source of Truth)
                      +---------+---------+
                                |
             +------------------+------------------+
             | Webhooks                            | GitOps Pull
             v                                     v
+-------------------------+            +-----------------------+
|  GitHub Actions (CI)    |            |     Argo CD (CD)      |
| - Lint (Ruff)           |            | - Continuous Sync     |
| - Unit Tests (Pytest)   |            | - Declarative State   |
| - Build & Push (GHCR)   |            | - Auto-Heal Drifts    |
+------------+------------+            +-----------+-----------+
             |                                     |
             v                                     v
  [ Container Registry ] -----------------> [ Kubernetes Cluster ]
   ghcr.io/cassideousxd/*                    platform namespace
                                                    |
       +--------------------------------------------+
       |
       v
+-----------------------------------------------------------------+
|                       Platform Services                         |
|  - API (FastAPI) [Replicas: 2]                                  |
|  - Auth Service (FastAPI / JWT)                                 |
|  - Worker Service (Redis Queue Consumer)                        |
|  - Frontend (Web Dashboard)                                     |
|  - Redis (State & Message Broker)                               |
+-------------------------------+---------------------------------+
                                | Metrics & Logs
                                v
+-----------------------------------------------------------------+
|                      Observability Stack                        |
|  - Prometheus (Metrics Scraping & SLI/SLO Alert Rules)          |
|  - Alertmanager (Routing, Grouping, Webhooks)                   |
|  - Grafana (6-Panel Executive & Operational Dashboard)          |
|  - Loki & Alloy (Distributed Log Aggregation)                   |
+-------------------------------+---------------------------------+
                                | Firing Alerts (remediation: auto)
                                v
+-----------------------------------------------------------------+
|                    Self-Healing Controller                      |
|  - Alertmanager v4 Webhook Receiver                             |
|  - Strict Safety Rails (Anti-Spoofing, Cooldown, Breaker)       |
|  - Automated Remediations (rollout_restart, restart_pod)        |
+-----------------------------------------------------------------+
```

---

## 🚀 Key Features

1. **Microservices Suite (`services/`)**:
   - `api`: High-performance asynchronous REST API with simulated chaos injection hooks (`/chaos/errors`, `/chaos/latency`).
   - `auth`: JWT token authentication and token verification provider.
   - `worker`: Background task processor reading jobs from Redis with exponential backoff.
   - `frontend`: Real-time dashboard providing end-to-end visibility.
   - `redis`: High-throughput in-memory cache and queue.

2. **GitOps & Continuous Deployment (`deploy/`)**:
   - **Argo CD**: Multi-environment overlays (`dev` and `prod`) using Kustomize.
   - **Automated CI/CD**: GitHub Actions matrix workflow building multi-architecture Docker containers (`linux/amd64`, `linux/arm64`) pushed to GHCR, followed by automated GitOps overlay tag bumping (`chore(deploy): bump dev images`).

3. **Full-Stack Observability (`monitoring/`)**:
   - **Prometheus**: Metrics collection from all services via `ServiceMonitor` resources.
   - **Grafana**: Pre-provisioned overview dashboard with panels for RPS, error rate, queue depth, pod restarts, system resources, and Loki live logs.
   - **Alertmanager**: Tuned alert routes with matchers for `remediation="auto"`, forwarding critical events directly to the self-healing controller.

4. **Self-Healing Remediation Controller (`controller/`)**:
   - **Webhook Receiver**: Asynchronous Alertmanager v4 webhook intake.
   - **Safety Rails Engine**:
     - *Namespace Scoping*: Drops foreign namespace alerts.
     - *Allowlist Enforcement*: Strictly targets allowlisted Deployments (`api`, `auth`, `worker`, `frontend`).
     - *Anti-Spoofing Pod Verification*: Traverses `Pod -> ReplicaSet -> Deployment` `ownerReferences` via Kubernetes API before executing pod deletions.
     - *Deduplication & Cooldown (240s)*: Prevents duplicate triggers while Prometheus metrics settle.
     - *Circuit Breaker*: Caps actions to a maximum of 3 remediations per service per hour; trips and escalates to human operator on continuous failures.
     - *Global In-Flight Cap*: Watchdog token mutex guaranteeing at most one mutating remediation executes concurrently across the cluster.
     - *Audit Logging*: Emits structured JSON events to `incidents.jsonl` and stdout.

---

## 🛠 Local Deployment & Development

### Prerequisites
- macOS or Linux
- Docker (Docker Desktop or Colima)
- `kind`, `kubectl`, `terraform`, `kustomize`

### Quick Start

1. **Start Local Cluster**:
   ```bash
   make cluster-up
   ```
2. **Bootstrap Argo CD & Platform**:
   ```bash
   make tf-bootstrap
   ```
3. **Verify Cluster & Pods**:
   ```bash
   kubectl -n platform get pods
   kubectl -n argocd get applications
   ```

---

## 🧹 Cleaning Up & Freeing Disk Space

If you are running locally on macOS with Colima:
- **Free CPU & Memory (pause cluster)**:
  ```bash
  colima stop
  ```
- **Reclaim all ~20 GB disk space (completely delete local VM)**:
  ```bash
  colima delete
  ```
> **Note**: Deleting Colima is completely safe after your code is pushed. All source code, Docker images, manifests, and dashboards live securely in your GitHub repository and GitHub Container Registry (GHCR).
