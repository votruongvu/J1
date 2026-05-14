# Integration tests

These tests run against real external services. They are
**skipped by default** so the unit-test suite (`pytest tests/`)
stays green on a laptop without infrastructure.

## When to run them

* Before shipping a change to evidence storage or the search path.
* When reviewing a Postgres-touching PR.
* Operators verifying the dev docker-compose stack.

The repo does NOT bundle a CI workflow that runs these tests — when
a CI pipeline is added, the recommended pattern is a separate job
that brings up the docker-compose `postgres` service and exports
`J1_TEST_POSTGRES_DSN` before invoking `pytest tests/integration/`.

## REST /search smoke — `test_rest_search_live.py`

Verifies the Phase 5/6 wire contract end-to-end against a running
dev stack. Skipped unless ``J1_TEST_LIVE_BASE_URL`` is set.

### Quick start

```bash
docker compose -f deploy/dev/docker-compose.yml up -d
# Ingest at least one document via the FE or POST /documents …
export J1_TEST_LIVE_BASE_URL=http://localhost:8000
export J1_TEST_LIVE_TENANT=acme
export J1_TEST_LIVE_PROJECT=alpha
.venv/bin/pytest tests/integration/test_rest_search_live.py -v
```

### What it verifies

* ``POST /search`` returns 200 with a ``hits`` array.
* Every hit carries ``snapshotId`` (Phase 5 wire field), ``chunkId``,
  and ``createdByRunId``.
* The endpoint does NOT require a ``run_id`` parameter — visibility
  is driven by the active-snapshot allowlist.
* A query made with a non-existent tenant returns no hits (or a
  4xx); never another tenant's data.

## Postgres FTS — `test_postgres_fts_live.py`

Verifies the Phase 3+ canonical evidence path against a live
PostgreSQL instance.

### Required environment

```bash
J1_TEST_POSTGRES_DSN=postgresql://USER:PASS@HOST:PORT/DBNAME
```

If `J1_TEST_POSTGRES_DSN` is unset, every test in the module is
skipped with `pytest.mark.skipif`.

### Quick start

```bash
# 1. Bring up the dev postgres service.
docker compose -f deploy/dev/docker-compose.yml up -d postgres

# 2. Point pytest at it.
export J1_TEST_POSTGRES_DSN=postgresql://j1:j1@localhost:5432/j1

# 3. Run.
.venv/bin/pytest tests/integration/test_postgres_fts_live.py -v
```

### What it verifies

For each test the module creates a fresh schema (`j1_test_<hex8>`)
to isolate concurrent / repeat runs; the schema is dropped on
teardown.

* **`test_live_postgres_round_trip_respects_every_filter`** — the
  combined contract:
  - schema bootstrap creates the table + GIN index + scope index
  - inserts produce searchable rows
  - search respects the explicit snapshot allowlist
  - wrong tenant returns no result
  - wrong project returns no result
  - wrong snapshot returns no result
  - multi-snapshot allowlist works
* **`test_live_postgres_delete_for_snapshot_only_drops_the_snapshot`**
  — `DELETE FROM j1.evidence_chunks WHERE snapshot_id=$1` removes
  only the targeted snapshot's rows; sibling snapshots survive.
* **`test_live_postgres_upsert_replaces_content_for_same_natural_key`**
  — re-running the same `(tenant, project, snapshot, artifact,
  chunk_id)` tuple REPLACES the content rather than duplicating it.
  Lets re-ingest be idempotent.

### What it does NOT verify

* Performance / load behaviour. Add a benchmark suite when needed.
* Concurrent writers from multiple workers (the adapter doesn't
  hold locks — Postgres's row-level locking handles that, but the
  test surface doesn't exercise it).
* Cross-database migrations from older J1 schemas. Phase 5 keeps
  no migration path; reset + re-ingest is the supported workflow.

## CI posture

The integration test is **opt-in only**. Adding CI verification of
Postgres FTS is straightforward when a pipeline is added:

```yaml
# .github/workflows/integration.yml (sketch)
jobs:
  postgres-fts:
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: j1
          POSTGRES_PASSWORD: j1
          POSTGRES_DB: j1
        ports: [5432:5432]
        options: >-
          --health-cmd="pg_isready -U j1"
          --health-interval=5s
          --health-timeout=5s
          --health-retries=10
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e .[dev,postgres]
      - env:
          J1_TEST_POSTGRES_DSN: postgresql://j1:j1@localhost:5432/j1
        run: pytest tests/integration/ -v
```

Until then: don't claim CI verifies Postgres FTS in any PR
description. The integration test passes locally; CI only runs
unit tests.
