"""Live REST /search smoke test — Phase 6.

Runs against a running dev stack when ``J1_TEST_LIVE_BASE_URL`` is
set; otherwise the entire module is SKIPPED. CI stays green without
external infrastructure.

How to run
----------

1. Bring up the dev stack:

    docker compose -f deploy/dev/docker-compose.yml up -d

2. Ingest at least one document (the rest of this test won't
   produce visible results without an active snapshot).

3. Export the base URL + auth header if you have one:

    export J1_TEST_LIVE_BASE_URL=http://localhost:8000
    export J1_TEST_LIVE_TENANT=acme
    export J1_TEST_LIVE_PROJECT=alpha
    # Optional API key:
    export J1_TEST_LIVE_AUTH="Bearer dev-key"

4. Run:

    .venv/bin/pytest tests/integration/test_rest_search_live.py -v

What it verifies
----------------

* ``POST /search`` returns 200 with a ``hits`` array.
* Each hit carries ``snapshotId`` (the Phase-5 wire field).
* The endpoint does NOT require a ``run_id`` parameter — Phase 6
  query path is snapshot-only.
* Tenant/project scoping is enforced via the ``X-Tenant-Id`` /
  ``X-Project-Id`` headers.
* A wrong tenant header returns either 0 hits or a 403/404 — never
  another tenant's data.

The test does NOT exercise ingestion — it assumes the operator has
already ingested data and a snapshot is active. Smoke purpose only.
"""

from __future__ import annotations

import os

import pytest

_BASE = os.environ.get("J1_TEST_LIVE_BASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not _BASE,
    reason=(
        "J1_TEST_LIVE_BASE_URL not set; live REST /search smoke test "
        "is skipped. See module docstring for setup."
    ),
)

httpx = pytest.importorskip("httpx")


def _headers(tenant: str | None = None, project: str | None = None) -> dict:
    h = {
        "X-Tenant-Id": tenant or os.environ.get(
            "J1_TEST_LIVE_TENANT", "acme",
        ),
        "X-Project-Id": project or os.environ.get(
            "J1_TEST_LIVE_PROJECT", "alpha",
        ),
        "Content-Type": "application/json",
    }
    auth = os.environ.get("J1_TEST_LIVE_AUTH", "").strip()
    if auth:
        h["Authorization"] = auth
    return h


def _post_search(query: str, *, tenant=None, project=None):
    resp = httpx.post(
        f"{_BASE}/search",
        headers=_headers(tenant=tenant, project=project),
        json={"query": query, "maxResults": 5},
        timeout=10.0,
    )
    return resp


def test_search_returns_snapshot_id_on_every_hit():
    """Phase 5 + 6 contract: every hit carries the snapshot lineage.
    Even if the query matches nothing, the response shape must be
    valid envelope JSON with a hits array."""
    resp = _post_search("the")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The bundled REST adapter wraps results in an envelope.
    data = body.get("data") or body
    hits = data.get("hits", [])
    # Don't fail on empty hits — operator may not have ingested
    # matching data — but if there are hits, every one MUST carry
    # snapshotId.
    for hit in hits:
        assert "snapshotId" in hit, hit
        # The new Phase-5 wire fields are also surfaced.
        assert "chunkId" in hit
        assert "createdByRunId" in hit


def test_search_does_not_require_run_id():
    """The /search request body has no run_id field — the endpoint
    must accept the request and return results based on the
    active-snapshot allowlist, not on caller-supplied run_id."""
    resp = httpx.post(
        f"{_BASE}/search",
        headers=_headers(),
        json={"query": "test", "maxResults": 1},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text


def test_search_excludes_wrong_tenant():
    """A query made with a non-existent tenant returns no hits
    (or a 4xx). Never returns another tenant's data."""
    resp = _post_search("anything", tenant="ghost-tenant-xxx")
    if resp.status_code == 200:
        body = resp.json()
        data = body.get("data") or body
        assert data.get("hits", []) == []
    else:
        assert resp.status_code in (401, 403, 404), resp.text
