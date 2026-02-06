# Azure Container Infrastructure Design

## PipesHub AI — Infrastructure Assessment & Recommendation

**Date:** 2026-02-06
**Scope:** Evaluate current VM-based Docker deployment and recommend optimal Azure infrastructure for the PipesHub AI platform, with focus on scaling, parallel execution, and cost-performance tradeoffs.

---

## Table of Contents

1. [Workload Analysis](#1-workload-analysis)
2. [Recommendation Summary](#2-recommendation-summary)
3. [Decision Matrix](#3-decision-matrix)
4. [Proposed Target Architecture](#4-proposed-target-architecture)
5. [Scaling Plan](#5-scaling-plan)
6. [Cost & Performance Discussion](#6-cost--performance-discussion)
7. [Migration Plan](#7-migration-plan)
8. [Appendix: Current Architecture Reference](#appendix-current-architecture-reference)

---

## 1. Workload Analysis

### 1.1 Runtime Type Classification

The platform is a **multi-service monolith** — architecturally microservices, but deployed today as a single container running 5 processes managed by a shell-based process monitor (`process_monitor.sh`).

| Service | Type | Port | Runtime |
|---------|------|------|---------|
| **Node.js API** | Always-on HTTP API server + WebSocket | 3000 | Node 20, Express, Socket.IO |
| **Connector Service** | Always-on API + Kafka consumer + Celery worker | 8088 | Python 3.10, FastAPI, Uvicorn |
| **Indexing Service** | Always-on Kafka consumer + document processor | 8091 | Python 3.10, FastAPI, Uvicorn |
| **Query Service** | Always-on API server (search + LLM) | 8000 | Python 3.10, FastAPI, Uvicorn |
| **Docling Service** | On-demand document parser | — | Python 3.10, Docling ML pipeline |

### 1.2 Execution Patterns

| Pattern | Services | Duration | Characteristic |
|---------|----------|----------|---------------|
| **Always-on HTTP** | Node.js API, Query, Connector | Indefinite | Low baseline, bursty on user activity |
| **Event-driven processing** | Indexing (Kafka consumer) | Seconds to 40 minutes per document | CPU-intensive, variable duration |
| **Scheduled tasks** | Celery Beat (Google Drive watch renewal) | Seconds | Every 12 hours, lightweight |
| **Crawling jobs** | BullMQ workers | Minutes to hours | Periodic, configurable schedule |
| **Real-time push** | Socket.IO (Node.js) | Persistent connections | Low CPU, high connection count |

### 1.3 CPU/RAM Profile

| Workload | CPU Pattern | Memory Pattern | Notes |
|----------|-------------|----------------|-------|
| API request handling | Low, async I/O-bound | ~500MB baseline | Express + FastAPI are efficient |
| Document parsing (Docling) | **High**, ML inference | **2–4GB** per document | Layout analysis, OCR, LibreOffice |
| Embedding generation | **High**, ML inference | **1–2GB** model loaded | sentence-transformers, bge-reranker |
| LLM API calls | Low (waiting on external API) | Low | I/O-bound, network-dependent |
| Kafka message processing | Low per message | Low | Semaphore-limited to 5 concurrent |
| OCR processing | **High** | Moderate | tesseract + ghostscript + ocrmypdf |

**Current resource allocation:** 10GB memory limit, 2GB shared memory, no CPU limit specified.
**Helm chart spec:** Requests 4 CPU / 4GB RAM, Limits 8 CPU / 8GB RAM.

### 1.4 External Dependencies

```
┌──────────────────────────────────────────────────────┐
│                 PipesHub AI Application               │
├──────────────────────────────────────────────────────┤
│                                                      │
│  Databases              Messaging        Config      │
│  ├── MongoDB            ├── Kafka        └── etcd    │
│  ├── ArangoDB           │   └── Zookeeper    (or     │
│  ├── Redis              └── BullMQ (Redis)   Redis)  │
│  └── Qdrant (Vector)                                 │
│                                                      │
│  External APIs          Storage                      │
│  ├── OpenAI/Anthropic   ├── Azure Blob               │
│  ├── Google Workspace   ├── AWS S3                   │
│  ├── Microsoft Graph    └── Local filesystem         │
│  ├── Slack/Teams                                     │
│  ├── Jira/Linear        Monitoring                   │
│  ├── GitHub/GitLab      └── Prometheus metrics push  │
│  ├── Salesforce                                      │
│  ├── ServiceNow                                      │
│  └── 15+ more connectors                            │
└──────────────────────────────────────────────────────┘
```

### 1.5 Failure Points & Retry Patterns

| Component | Failure Mode | Retry Strategy | Current Implementation |
|-----------|-------------|----------------|----------------------|
| External API calls | HTTP errors, rate limits | 3 attempts, exponential backoff 4–15s | `tenacity` library |
| Kafka consumer | Broker disconnect, rebalance | 8 retries, 100ms–30s exponential | `kafkajs` built-in |
| Redis connection | Network timeout | Exponential backoff 50ms–2s | `ioredis` retry strategy |
| Process crash | Service dies | process_monitor.sh restarts (20s check interval) | Shell-based watchdog |
| Document processing | Timeout/OOM | 40-minute timeout, no retry at processing level | Relies on Kafka redelivery |
| Health checks | Service not ready | 30 retries × 2s (60s max) | Startup health probes |

### 1.6 State Assessment

**The application layer is stateless.** All state is externalized:

- Sessions → Redis (1-hour TTL)
- User data → MongoDB
- Entity graphs → ArangoDB
- Embeddings → Qdrant
- Configuration → etcd / Redis KV store
- Event stream → Kafka
- File artifacts → Azure Blob / S3 / local mount

No sticky sessions. No in-memory state that survives restarts (aside from ML model weights loaded at startup).

**Implication:** Any service instance can handle any request. Horizontal scaling is architecturally safe.

**Caveat:** ML model loading at startup takes 30–90 seconds (sentence-transformers, spaCy, NLTK data). This makes cold starts expensive.

---

## 2. Recommendation Summary

### **Recommended: Option D — Hybrid Azure Container Apps + Container Apps Jobs**

Decompose the monolithic container into individual services on **Azure Container Apps** (for always-on workloads) and **Azure Container Apps Jobs** (for burst document processing), with managed Azure services replacing self-hosted infrastructure.

**Why not the other options:**

| Option | Verdict | Reasoning |
|--------|---------|-----------|
| **A: Keep VM + Docker** | ❌ Not recommended | No autoscaling, SPOF, manual ops, over-provisioned for baseline, under-provisioned for burst |
| **B: Container Apps only** | ⚠️ Close, but suboptimal | Good for always-on services, but document processing (40-min jobs, high CPU) wastes resources when idle and doesn't benefit from always-on billing |
| **C: Container Apps Jobs only** | ❌ Not suitable | Jobs are designed for run-to-completion tasks; the API servers and Kafka consumers need to run continuously |
| **D: Hybrid (Container Apps + Jobs)** | ✅ Recommended | Always-on API/query services on Container Apps with min replicas; burst document processing on Jobs with event-driven scaling |

---

## 3. Decision Matrix

### 3.1 Comparative Analysis

| Criteria | VM + Docker | Azure Container Apps | ACA Jobs | Hybrid (ACA + Jobs) |
|----------|-------------|---------------------|----------|---------------------|
| **Auto-scaling** | ❌ Manual | ✅ HTTP/KEDA triggers | ✅ Event-driven | ✅ Best of both |
| **Parallel execution** | ⚠️ Limited by single VM | ✅ Multiple revisions/replicas | ✅ Parallel job executions | ✅ Independent scaling per tier |
| **Always-on baseline** | ✅ Constant (but wasteful) | ✅ Min replicas | ❌ Not designed for this | ✅ ACA for always-on |
| **Burst traffic** | ❌ Capacity ceiling | ✅ Scale-out | ✅ Designed for burst | ✅ Jobs for burst processing |
| **High parallelism** | ❌ Single-machine bound | ✅ Up to 300 replicas | ✅ Up to 100 parallel executions | ✅ Unconstrained |
| **Cold start impact** | ✅ None (always running) | ⚠️ 30–90s for ML models | ⚠️ 30–90s per job | ⚠️ Mitigated with min replicas + pre-warmed pools |
| **Cost (idle)** | ❌ Full VM cost 24/7 | ✅ Scale to 0 or min | ✅ Zero when idle | ✅ Min replicas for API, zero for jobs |
| **Cost (peak)** | ❌ Must provision for peak | ✅ Pay for usage | ✅ Pay per execution | ✅ Optimal cost curve |
| **Ops complexity** | ❌ Manual everything | ✅ Managed platform | ✅ Managed platform | ⚠️ More services to configure |
| **Deployment/rollback** | ❌ Manual docker pull | ✅ Revisions, traffic split | ✅ Version management | ✅ Full revision control |
| **Monitoring** | ❌ DIY (Prometheus only) | ✅ Azure Monitor, Log Analytics | ✅ Azure Monitor | ✅ Unified Azure Monitor |
| **Reliability (SPOF)** | ❌ Single VM, single container | ✅ Multi-replica, zone-redundant | ✅ Managed retry + parallelism | ✅ No SPOF |
| **Secret management** | ⚠️ Env vars on VM | ✅ Azure Key Vault integration | ✅ Azure Key Vault integration | ✅ Centralized Key Vault |
| **Infra drift** | ❌ High risk | ✅ IaC via Bicep/Terraform | ✅ IaC | ✅ IaC |
| **HA / Upgrades** | ❌ Downtime for upgrades | ✅ Zero-downtime revisions | ✅ N/A (jobs are ephemeral) | ✅ Zero-downtime |

### 3.2 Scoring (1–5, higher is better)

| Criteria (Weight) | VM + Docker | ACA Only | ACA Jobs Only | Hybrid |
|-------------------|-------------|----------|---------------|--------|
| Scaling (25%) | 1 | 4 | 3 | **5** |
| Cost efficiency (20%) | 2 | 4 | 4 | **5** |
| Ops simplicity (20%) | 2 | 4 | 4 | **3** |
| Reliability (15%) | 2 | 4 | 3 | **5** |
| Parallel execution (10%) | 1 | 4 | 5 | **5** |
| Security (10%) | 2 | 4 | 4 | **4** |
| **Weighted Total** | **1.7** | **4.0** | **3.7** | **4.5** |

---

## 4. Proposed Target Architecture

### 4.1 High-Level Architecture

```
                    ┌─────────────────────────────────────────────────┐
                    │           Azure Container Apps Environment       │
                    │                                                  │
    Internet ──►    │  ┌──────────────────────────────────────────┐   │
                    │  │  Ingress (HTTPS, custom domain, TLS)     │   │
                    │  └────────────┬─────────────────────────────┘   │
                    │               │                                  │
                    │  ┌────────────▼─────────────┐                   │
                    │  │   nodejs-api (ACA)        │ ◄── min: 1       │
                    │  │   Port 3000               │     max: 5       │
                    │  │   Express + Socket.IO      │     scale: HTTP  │
                    │  │   + Frontend static files  │                  │
                    │  └──┬─────────┬──────────┬───┘                  │
                    │     │         │          │                       │
                    │     ▼         ▼          ▼                       │
                    │  ┌──────┐ ┌──────┐ ┌──────────┐                │
                    │  │query │ │conn- │ │indexing   │                │
                    │  │(ACA) │ │ector │ │(ACA)     │ ◄── min: 1     │
                    │  │:8000 │ │(ACA) │ │:8091     │     max: 10    │
                    │  │min:1 │ │:8088 │ │Kafka     │     scale:     │
                    │  │max:3 │ │min:1 │ │consumer  │     Kafka lag  │
                    │  └──────┘ │max:3 │ └──────────┘                │
                    │           └──────┘                              │
                    │                                                  │
                    │  ┌──────────────────────────────────────────┐   │
                    │  │   Document Processing (ACA Jobs)         │   │
                    │  │                                          │   │
                    │  │   ┌─────────┐ ┌─────────┐ ┌─────────┐  │   │
                    │  │   │ docling │ │ docling │ │ docling │  │   │
                    │  │   │ job-1  │ │ job-2  │ │ job-N  │  │   │
                    │  │   └─────────┘ └─────────┘ └─────────┘  │   │
                    │  │   Triggered by: Kafka / Azure Queue     │   │
                    │  │   Parallel: up to 10                    │   │
                    │  │   Timeout: 45 min                       │   │
                    │  └──────────────────────────────────────────┘   │
                    │                                                  │
                    └─────────────────────────────────────────────────┘
                                          │
                    ┌─────────────────────────────────────────────────┐
                    │           Azure Managed Services                 │
                    │                                                  │
                    │  ┌────────────┐  ┌────────────┐                │
                    │  │ Azure      │  │ Azure      │                │
                    │  │ Cosmos DB  │  │ Cache for  │                │
                    │  │ (Mongo API)│  │ Redis      │                │
                    │  └────────────┘  └────────────┘                │
                    │                                                  │
                    │  ┌────────────┐  ┌────────────┐                │
                    │  │ Azure      │  │ Azure      │                │
                    │  │ Event Hubs │  │ Key Vault  │                │
                    │  │ (Kafka API)│  │            │                │
                    │  └────────────┘  └────────────┘                │
                    │                                                  │
                    │  ┌────────────┐  ┌────────────┐                │
                    │  │ Qdrant on  │  │ ArangoDB   │                │
                    │  │ ACA (ded.) │  │ on ACA     │                │
                    │  │ or Qdrant  │  │ (dedicated)│                │
                    │  │ Cloud      │  │            │                │
                    │  └────────────┘  └────────────┘                │
                    │                                                  │
                    └─────────────────────────────────────────────────┘
```

### 4.2 Service Decomposition

| Service | Deployment Target | Scaling Model | Min/Max Replicas |
|---------|-------------------|---------------|------------------|
| **nodejs-api** | ACA (always-on) | HTTP concurrent requests | 1 / 5 |
| **query-service** | ACA (always-on) | HTTP concurrent requests | 1 / 3 |
| **connector-service** | ACA (always-on) | HTTP + Kafka consumer group | 1 / 3 |
| **indexing-service** | ACA (always-on) | Kafka consumer lag | 1 / 10 |
| **docling-worker** | ACA Job (event-driven) | Queue length | 0 / 10 |
| **celery-beat** | ACA (always-on, single) | None (singleton) | 1 / 1 |

### 4.3 Infrastructure Services

| Current (Self-Hosted) | Target (Azure Managed) | Rationale |
|-----------------------|------------------------|-----------|
| MongoDB container | **Azure Cosmos DB for MongoDB** | Managed, auto-scale, backup, geo-replication |
| Redis container | **Azure Cache for Redis** | Managed HA, clustering, persistence |
| Kafka + Zookeeper | **Azure Event Hubs (Kafka API)** | Managed, Kafka wire protocol compatible, KEDA integration |
| etcd | **Remove** — migrate to Redis KV | Migration already in progress in codebase; eliminates a component |
| Qdrant container | **Qdrant Cloud** or **ACA dedicated container** | Qdrant Cloud for managed; ACA container if cost-sensitive |
| ArangoDB container | **ACA dedicated container** with persistent volume | No managed Azure equivalent; run as a dedicated ACA app with persistent storage |

---

## 5. Scaling Plan

### 5.1 Autoscaling Rules

```yaml
# nodejs-api: Scale on HTTP concurrency
scaling:
  minReplicas: 1
  maxReplicas: 5
  rules:
    - name: http-scaling
      http:
        metadata:
          concurrentRequests: "50"

# query-service: Scale on HTTP concurrency (heavier per-request due to LLM)
scaling:
  minReplicas: 1
  maxReplicas: 3
  rules:
    - name: http-scaling
      http:
        metadata:
          concurrentRequests: "10"  # Lower threshold — LLM calls are slow

# indexing-service: Scale on Kafka consumer lag
scaling:
  minReplicas: 1
  maxReplicas: 10
  rules:
    - name: kafka-lag
      custom:
        type: kafka  # KEDA scaler
        metadata:
          bootstrapServers: "<event-hubs-endpoint>"
          consumerGroup: "record_consumer_group"
          topic: "record-events"
          lagThreshold: "10"  # Scale up when >10 unprocessed messages

# docling-worker (ACA Job): Event-driven execution
trigger:
  type: event
  scale:
    maxExecutions: 10        # Up to 10 parallel document processing jobs
    pollingInterval: 15      # Check for new work every 15 seconds
    minExecutions: 0         # Scale to zero when idle
  rules:
    - name: queue-trigger
      type: azure-queue      # Azure Storage Queue or Service Bus
      metadata:
        queueLength: "1"     # 1 job per message

# connector-service: Scale on combination
scaling:
  minReplicas: 1
  maxReplicas: 3
  rules:
    - name: http-scaling
      http:
        metadata:
          concurrentRequests: "30"
```

### 5.2 Parallel Execution Strategy

#### Problem: Multiple tasks processing simultaneously without conflicts

**Current state:** Single container, `asyncio.Semaphore(5)` limits to 5 concurrent Kafka messages, `aiolimiter` caps at 2 new tasks/second.

**Target state:** Distributed processing across multiple replicas.

#### Parallel Execution Design

```
                     Kafka Topic: record-events
                     (Partitioned: 10 partitions)
                              │
              ┌───────────────┼───────────────┐
              ▼               ▼               ▼
     ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
     │ indexing-1   │ │ indexing-2   │ │ indexing-N   │
     │ partitions   │ │ partitions   │ │ partitions   │
     │ 0,1,2        │ │ 3,4,5        │ │ 6,7,8,9      │
     │              │ │              │ │              │
     │ semaphore(5) │ │ semaphore(5) │ │ semaphore(5) │
     │ rate(2/s)    │ │ rate(2/s)    │ │ rate(2/s)    │
     └──────┬───────┘ └──────┬───────┘ └──────┬───────┘
            │                │                │
            ▼                ▼                ▼
     Heavy docs → Dispatch to ACA Jobs (docling-worker)
     Light docs → Process in-place
```

**Key mechanisms:**

| Concern | Mechanism | Implementation |
|---------|-----------|----------------|
| **No duplicate processing** | Kafka consumer groups | Each partition assigned to exactly one consumer. ACA replicas join the same consumer group → Kafka rebalances partitions automatically |
| **Rate limiting** | Per-replica semaphore + rate limiter | Existing `asyncio.Semaphore(5)` and `aiolimiter(2/s)` stay in place per replica. Global rate is `N_replicas × 2/s` |
| **External API rate limits** | Distributed rate limiter via Redis | Use Redis `INCR` + `EXPIRE` (sliding window) for shared rate limit counters. Example: OpenAI 60 RPM → Redis key `ratelimit:openai:{minute}` |
| **Idempotency** | Message deduplication + idempotent writes | Already implemented: message dedup tracking in Kafka consumer. Database writes use upsert semantics (ArangoDB, MongoDB). Vector upserts in Qdrant by document ID |
| **Distributed locking** | Redis `SET NX EX` (advisory locks) | For connector sync operations that must be singleton (e.g., Google Drive full sync per org). Use `SETNX` with TTL for lease-based locking |
| **Job completion tracking** | Record status in MongoDB | Existing status machine: `NEW_RECORD` → `QUEUED` → `IN_PROGRESS` → `COMPLETED`/`FAILED`. Recovery on startup already implemented |
| **Retries** | Kafka redelivery + dead letter queue | Failed messages → retry topic (3 attempts) → dead letter topic (manual inspection) |

### 5.3 Kafka Partition Strategy

For effective horizontal scaling of the indexing service:

- **Increase partitions** on `record-events` topic to at least 10 (currently 1)
- Partition key: `orgId` or `connectorId` — ensures all records for one org/connector go to the same partition (preserves ordering where needed)
- Azure Event Hubs supports up to 32 partitions on Standard tier, 100+ on Premium

---

## 6. Cost & Performance Discussion

### 6.1 Cost Comparison (Qualitative — US East region)

#### Scenario A: Low usage baseline (small team, ~100 docs/day)

| Component | VM + Docker | Hybrid ACA + Jobs |
|-----------|-------------|-------------------|
| Compute | D4s_v5 (4 vCPU, 16GB): ~$140/mo | ACA: ~$80/mo (4 always-on containers, consumption plan) |
| Databases | Self-hosted (included in VM) | Cosmos DB (400 RU/s): ~$25/mo, Azure Cache Redis (C0): ~$16/mo |
| Messaging | Self-hosted Kafka (included in VM) | Event Hubs Basic: ~$11/mo |
| Docling jobs | Runs inside VM (idle most of time) | ACA Jobs: ~$5/mo (pay only when processing) |
| **Total estimate** | **~$140/mo** (under-utilizing) | **~$137/mo** (right-sized) |
| **Key difference** | Paying for idle capacity | Paying for actual usage, but more line items |

#### Scenario B: Medium usage (growing team, ~1,000 docs/day, burst connector syncs)

| Component | VM + Docker | Hybrid ACA + Jobs |
|-----------|-------------|-------------------|
| Compute | D8s_v5 (8 vCPU, 32GB): ~$280/mo + may need 2nd VM | ACA: ~$150/mo (auto-scales 1→5 replicas) |
| Databases | Need separate DB VM or managed services anyway | Cosmos DB (1000 RU/s): ~$60/mo, Redis (C1): ~$42/mo |
| Messaging | Kafka often OOMs at this scale on shared VM | Event Hubs Standard: ~$22/mo |
| Docling jobs | Competes for CPU with API serving | ACA Jobs: ~$25/mo (parallel processing, no API impact) |
| **Total estimate** | **~$400–560/mo** (two VMs) | **~$299/mo** |
| **Key difference** | **API latency degrades during bulk indexing** | **API and processing scale independently** |

#### Scenario C: High usage (enterprise, ~10,000 docs/day, 50+ connectors, parallel syncs)

| Component | VM + Docker | Hybrid ACA + Jobs |
|-----------|-------------|-------------------|
| Compute | Multiple VMs + manual load balancing: ~$800+/mo | ACA: ~$400/mo (auto-scales to 10+ replicas) |
| Databases | Must use managed services at this scale | Cosmos DB (5000 RU/s): ~$290/mo, Redis (C2): ~$170/mo |
| Messaging | Self-managed Kafka cluster: ~$300/mo (3 brokers) | Event Hubs Standard (10 TU): ~$220/mo |
| Docling jobs | Dedicated processing VMs: ~$300/mo | ACA Jobs: ~$100/mo (burst to 10 parallel) |
| **Total estimate** | **~$1,400+/mo** + significant ops burden | **~$1,180/mo** |
| **Key difference** | **Manual scaling, ops team required** | **Auto-scales, ops-light** |

### 6.2 Cost Optimization Levers

| Lever | Impact | Effort |
|-------|--------|--------|
| **ACA consumption plan** (vs. dedicated) | 30–50% savings at low-medium scale | Low (default plan) |
| **Azure Reserved Instances** for Cosmos DB | 20–35% savings | Low (1-year commitment) |
| **Scale to 0** for query-service during off-hours | 10–20% compute savings | Low (set minReplicas: 0 with 30s cold start tradeoff) |
| **Spot instances** for ACA Jobs (docling) | 60–90% savings on burst compute | Medium (handle preemption) |
| **Move Qdrant to Qdrant Cloud free tier** initially | $0 for small collections | Low |
| **Remove etcd** (use Redis KV instead) | Eliminate one service | Low (migration in progress) |

### 6.3 Performance Characteristics

| Metric | VM + Docker | Hybrid ACA + Jobs |
|--------|-------------|-------------------|
| API P50 latency | Good (no cold start) | Good (min replicas = 1) |
| API P99 latency during bulk indexing | **Degraded** (shared CPU) | Unaffected (independent scaling) |
| Document processing throughput | 5 concurrent (fixed) | 5 per replica × N replicas + parallel jobs |
| Max parallel doc processing | ~5 (single VM) | 50+ (10 replicas × 5 semaphore) |
| Cold start time | 0 (always running) | 30–90s for ML-heavy services |
| Recovery after failure | 20s (process monitor) | Seconds (ACA health probes + auto-restart) |

---

## 7. Migration Plan

### Principles

- **Phased approach** — no big-bang migration
- **Rollback-safe** — VM remains operational until new infra is validated
- **Minimal code changes** — use environment variables and config to switch targets
- **Zero downtime** — DNS cutover with traffic splitting

### Phase 0: Preparation (pre-migration)

**Goal:** Prepare codebase and infrastructure for decomposition.

| Step | Action | Risk | Code Change |
|------|--------|------|-------------|
| 0.1 | Increase Kafka `record-events` partitions to 10 | Low | Config only |
| 0.2 | Complete etcd → Redis KV migration | Low | Already in progress |
| 0.3 | Build individual Docker images for each service using existing Dockerfiles | Low | CI/CD pipeline update |
| 0.4 | Add distributed rate limiting via Redis (for external APIs) | Medium | New utility module |
| 0.5 | Add Redis-based distributed locks for singleton operations | Medium | New utility module |
| 0.6 | Verify each service starts and runs independently (not via process_monitor.sh) | Medium | Test individual Dockerfiles |
| 0.7 | Set up Azure Container Apps Environment + Azure Key Vault via IaC (Bicep/Terraform) | Low | New IaC files |

### Phase 1: Managed Data Services

**Goal:** Move stateful infrastructure to Azure managed services. VM still runs the application.

```
Before:  VM [ App + MongoDB + Redis + Kafka + Zookeeper + etcd + Qdrant + ArangoDB ]
After:   VM [ App + ArangoDB ] ──► Azure Cosmos DB, Azure Cache for Redis, Azure Event Hubs
```

| Step | Action | Validation | Rollback |
|------|--------|------------|----------|
| 1.1 | Deploy Azure Cosmos DB for MongoDB API | Connection test from VM | Point back to local MongoDB |
| 1.2 | Migrate MongoDB data using `mongodump`/`mongorestore` | Data integrity check | Restore from dump |
| 1.3 | Switch `MONGO_URI` to Cosmos DB endpoint | Run integration tests | Revert env var |
| 1.4 | Deploy Azure Cache for Redis | Connection test | Point back to local Redis |
| 1.5 | Migrate Redis data (sessions will auto-populate, config via KV migration script) | Session test | Revert env var |
| 1.6 | Deploy Azure Event Hubs with Kafka API enabled | Producer/consumer test | Revert Kafka broker config |
| 1.7 | Switch `KAFKA_BROKERS` to Event Hubs endpoint | Monitor message flow | Revert to self-hosted Kafka |
| 1.8 | Remove local MongoDB, Redis, Kafka, Zookeeper, etcd containers | Monitor for 48 hours | Restart containers if issues |

**Duration estimate:** Not provided per constraints — steps are incremental and independently reversible.

### Phase 2: Application Services to ACA

**Goal:** Move application containers from VM to Azure Container Apps.

```
Before:  VM [ App (monolith container) + ArangoDB ]
After:   ACA [ nodejs-api, query, connector, indexing, celery-beat ]
         ACA [ ArangoDB (dedicated) ]
         VM  [ Standby — ready for rollback ]
```

| Step | Action | Validation |
|------|--------|------------|
| 2.1 | Deploy ArangoDB as dedicated ACA app with persistent volume | Data migration + integrity check |
| 2.2 | Deploy `nodejs-api` to ACA with min=1, max=5 | Health check + API smoke tests |
| 2.3 | Deploy `query-service` to ACA with min=1, max=3 | Search query tests |
| 2.4 | Deploy `connector-service` to ACA with min=1, max=3 | Connector sync tests |
| 2.5 | Deploy `indexing-service` to ACA with min=1, max=10, KEDA Kafka trigger | Index a batch of documents, verify completion |
| 2.6 | Deploy `celery-beat` as singleton ACA app (replica=1) | Verify scheduled task execution |
| 2.7 | Configure Azure Key Vault references for all secrets | Verify services start with Key Vault secrets |
| 2.8 | Set up custom domain + TLS on ACA ingress | HTTPS endpoint test |
| 2.9 | DNS cutover: point domain to ACA ingress (or use traffic splitting) | Monitor error rates, latency |
| 2.10 | Keep VM on standby for 1 week | Quick rollback if needed |
| 2.11 | Decommission VM | — |

### Phase 3: Document Processing Jobs

**Goal:** Move heavy document processing to ACA Jobs for cost-efficient burst scaling.

| Step | Action | Validation |
|------|--------|------------|
| 3.1 | Create Azure Storage Queue (or Service Bus queue) for document processing jobs | Queue connectivity test |
| 3.2 | Modify indexing-service: for heavy documents (PDF, large files), dispatch to queue instead of processing in-place | Unit tests for dispatch logic |
| 3.3 | Build docling-worker Docker image (standalone, reads from queue, processes document, writes result) | Process sample documents |
| 3.4 | Deploy ACA Job with event trigger (queue) and max 10 parallel executions | Load test with 50 documents |
| 3.5 | Add dead letter queue for failed processing attempts | Verify DLQ receives failed messages |
| 3.6 | Monitor and tune: adjust parallel execution count, timeouts, and resource limits | Performance benchmarking |

### Phase 4: Observability & Hardening

| Step | Action |
|------|--------|
| 4.1 | Configure Azure Monitor and Log Analytics workspace for all ACA services |
| 4.2 | Set up alerts: error rate > 5%, P99 latency > 5s, Kafka lag > 100 messages |
| 4.3 | Configure Azure Container Apps health probes (liveness + readiness) for all services |
| 4.4 | Set up Azure Dashboard with key metrics: request rate, error rate, Kafka lag, job queue depth |
| 4.5 | Enable Azure Container Apps revision management for blue-green deployments |
| 4.6 | Implement CI/CD pipeline (GitHub Actions → ACR → ACA deployment) |
| 4.7 | Document runbooks for common operational tasks |

---

## Appendix: Current Architecture Reference

### Current Deployment Topology (As-Is)

```
┌──────────────────────────────────────────────────┐
│                Azure VM (Single)                  │
│                                                   │
│  ┌─────────────────────────────────────────────┐ │
│  │  Docker Container: pipeshub-ai              │ │
│  │  (process_monitor.sh manages 5 processes)   │ │
│  │                                             │ │
│  │  ┌──────────┐ ┌──────────┐ ┌───────────┐  │ │
│  │  │ Node.js  │ │ Query    │ │ Connector │  │ │
│  │  │ API:3000 │ │ Svc:8000 │ │ Svc:8088  │  │ │
│  │  └──────────┘ └──────────┘ └───────────┘  │ │
│  │  ┌──────────┐ ┌──────────┐                │ │
│  │  │ Indexing  │ │ Docling  │                │ │
│  │  │ Svc:8091 │ │ (ML)     │                │ │
│  │  └──────────┘ └──────────┘                │ │
│  └─────────────────────────────────────────────┘ │
│                                                   │
│  ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐ │
│  │MongoDB │ │ Redis  │ │ArangoDB│ │  Qdrant  │ │
│  └────────┘ └────────┘ └────────┘ └──────────┘ │
│  ┌────────┐ ┌────────┐ ┌────────┐              │
│  │ Kafka  │ │Zookpr  │ │  etcd  │              │
│  └────────┘ └────────┘ └────────┘              │
│                                                   │
│  Memory limit: 10GB    Shared memory: 2GB        │
│  Single point of failure ⚠️                       │
└──────────────────────────────────────────────────┘
```

### Key Risks in Current Architecture

| Risk | Impact | Likelihood |
|------|--------|------------|
| VM failure | Complete service outage | Medium |
| OOM (document processing spike) | All services crash | High |
| No horizontal scaling | Cannot handle burst traffic | Certain at growth |
| Manual deployment | Human error, slow rollback | High |
| Secrets in env vars on VM | Credential exposure | Medium |
| No autoscaling | Over-provisioned at idle, under-provisioned at peak | Certain |
| process_monitor.sh 20s check interval | Up to 20s undetected downtime per service | Low-Medium |
| Kafka single partition | Cannot parallelize message processing | Certain at scale |

---

*This document should be reviewed and updated as the migration progresses. Each phase can be executed independently and includes rollback mechanisms.*
