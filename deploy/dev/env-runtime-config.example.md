# `.env` keys introduced by Phase 1 of the snapshot-centered refactor

This file documents the new env vars the unified RuntimeConfig
(`src/j1/config/runtime.py`) consumes. Copy any block you want to
override into your `.env` — every key has a working default for
the DEV docker-compose stack, so the file is **additive**: nothing
breaks if you leave it alone.

The `.env.example` checked into the repo is the live source; this
document is the proposal of which keys to add and why. Once your
operator approves, paste the block into `.env.example`.

---

## Profile

```bash
# dev | prod. dev allows local fallbacks; prod fails fast.
J1_RUNTIME_PROFILE=dev
```

## Data root

```bash
# Top-level directory inside the container. Each named volume
# mounts under here (raganything, mineru, benchmarks).
J1_DATA_ROOT=/var/lib/j1
```

## Metadata (Postgres)

```bash
# The default docker-compose stack runs postgres:16 with user=j1,
# password=j1, database=j1. The temporal database lives in the same
# postgres instance under db=temporal.
J1_METADATA_BACKEND=postgres
J1_METADATA_DSN=postgresql://j1:j1@postgres:5432/j1
J1_METADATA_SCHEMA=j1
J1_POSTGRES_USER=j1
J1_POSTGRES_PASSWORD=j1
J1_POSTGRES_PORT=5432
```

## Artifact storage (MinIO / S3)

```bash
J1_ARTIFACT_BACKEND=s3
J1_ARTIFACT_ENDPOINT=http://minio:9000
J1_ARTIFACT_REGION=us-east-1
J1_ARTIFACT_BUCKET=j1-artifacts
J1_ARTIFACT_ACCESS_KEY=j1-dev
J1_ARTIFACT_SECRET_KEY=j1-dev-secret
J1_ARTIFACT_USE_TLS=false
J1_MINIO_PORT=9000
J1_MINIO_CONSOLE_PORT=9001
```

## Cache (Redis)

```bash
J1_CACHE_BACKEND=redis
J1_CACHE_URL=redis://redis:6379/0
J1_REDIS_PORT=6379
```

## Vector store (Qdrant)

```bash
J1_VECTOR_BACKEND=qdrant
J1_VECTOR_URL=http://qdrant:6333
J1_VECTOR_API_KEY=
J1_VECTOR_COLLECTION_PREFIX=j1
J1_QDRANT_PORT=6333
J1_QDRANT_GRPC_PORT=6334
```

## Graph store (Neo4j)

```bash
J1_GRAPH_BACKEND=neo4j
J1_GRAPH_URL=bolt://neo4j:7687
J1_GRAPH_USER=neo4j
J1_GRAPH_PASSWORD=neo4j-dev
J1_GRAPH_DATABASE=neo4j
J1_NEO4J_HTTP_PORT=7474
J1_NEO4J_BOLT_PORT=7687
```

## Evidence search (Postgres FTS)

```bash
# Reuses the metadata DSN by default. Set explicitly if you want to
# route FTS to a dedicated postgres instance.
J1_EVIDENCE_BACKEND=postgres_fts
# J1_EVIDENCE_DSN=postgresql://j1:j1@fts:5432/j1_evidence
```

## RAGAnything workspace

```bash
J1_RAG_BACKEND=raganything
J1_RAG_WORKDIR=/var/lib/j1/raganything
J1_LIGHTRAG_WORKDIR=/var/lib/j1/raganything/lightrag
J1_MINERU_WORKDIR=/var/lib/j1/mineru
```

## Concurrency

```bash
J1_WORKER_MAX_CONCURRENT_ACTIVITIES=8
J1_RAG_MAX_CONCURRENT_DOCUMENTS=4
```

## Benchmark / timing

```bash
J1_BENCHMARK_STAGE_TIMING=false
J1_BENCHMARK_INGESTION=false
J1_BENCHMARK_OUTPUT_PATH=/var/lib/j1/benchmarks
```

## Cleanup / retention

```bash
# DEV default: hard-delete on, no retention window.
# PROD default: hard-delete off, retention configurable.
J1_CLEANUP_HARD_DELETE=true
# J1_CLEANUP_RETENTION_DAYS=14
```

---

## Notes for the operator

* **Same keys in DEV and PROD** — only values differ. PROD typically:
  - `J1_RUNTIME_PROFILE=prod`
  - Drop the dev-fallback backends (no `sqlite_local`, `memory`, `local_fs`,
    `embedded_lightrag`, `sqlite_fts5` — `validate()` will refuse them).
  - Supply real DSNs / URLs.
* **Per-provider fallbacks** — DEV can switch any backend to its dev-only
  variant (e.g. `J1_CACHE_BACKEND=memory`) when the docker stack isn't
  running. The validator skips `is_configured` checks for backends that
  don't need external services.
* **Reset** — `scripts/dev/reset_docker.sh` wipes every named volume in
  `deploy/dev/docker-compose.yml`. Add `--rmi` to also drop built images.
