"""``POST /dev/query-trace`` endpoint tests.

The endpoint is the developer/operator surface for the new
SmartQueryOrchestrator. It returns the full ``QueryTrace`` JSON so
operators can answer "why did the query fail" without instrumentation.

Tests verify the wiring: optional dependency, missing-input
validation, scope plumbing, and the returned trace shape."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from j1.adapters.rest.app import create_rest_api
from j1.integration import ApplicationFacade
from j1.query.answer_synthesizer import SynthesisRequest
from j1.query.orchestrator import SmartQueryOrchestrator
from j1.query.query_plan import (
    EvidenceCandidate,
    RetrievalRouteKind,
)


@pytest.fixture
def application_facade() -> ApplicationFacade:
    """A minimal facade — the /dev/query-trace endpoint doesn't
    consume any of the facade services, so empty stubs are fine."""
    return ApplicationFacade(
        ingestion=None,
        retrieval=None,
        citation_lookup=None,
        source_lookup=None,
        feedback=None,
        event_publisher=None,
        search=None,
    )


_FAILED_QUERY = (
    "How do the deliverables evolve from conceptual engineering "
    "through 60%, 90%, and 100% design, and which cost estimate "
    "class is associated with each design stage?"
)


# ---- Test fixtures ---------------------------------------------


class _DictRoute:
    def __init__(self, kind, per_label):
        self.kind = kind
        self._per_label = per_label

    def execute(self, job, ctx):
        return list(self._per_label.get(job.label, []))


def _cand(*, artifact_id, body, route=RetrievalRouteKind.RAGANYTHING, score=0.7):
    return EvidenceCandidate(
        route=route, artifact_id=artifact_id, artifact_kind="chunk",
        chunk_id=f"c-{artifact_id}", text_preview=body[:80],
        score=score, matched_anchors=(),
        run_id="run-1", document_id="doc-1", project_id="p",
        extra={"body": body},
    )


def _good_orchestrator():
    routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [
                _cand(artifact_id="A",
                      body="60% design deliverables include drawings."),
                _cand(artifact_id="B",
                      body="90% design deliverables include specs."),
                _cand(artifact_id="C",
                      body="100% design deliverables include final set."),
                _cand(artifact_id="D",
                      body="conceptual engineering feasibility."),
                _cand(artifact_id="E",
                      body="cost estimate class 3 for design stages."),
            ]},
        ),
        RetrievalRouteKind.BM25: _DictRoute(
            RetrievalRouteKind.BM25, {},
        ),
    }

    def _llm(req: SynthesisRequest) -> str:
        return (
            "| Stage | deliverables | cost estimate class | Citation |\n"
            "| --- | --- | --- | --- |\n"
            "| 60% | drawings | Class 3 | [#1] [#5] |\n"
            "| 90% | specs | n/a | [#2] |\n"
            "| 100% design | final set | n/a | [#3] |\n"
            "| conceptual engineering | feasibility | n/a | [#4] |"
        )
    return SmartQueryOrchestrator.from_components(
        routes=routes, llm=_llm,
    )


# ---- 503 when not wired ----------------------------------------


def test_returns_503_when_orchestrator_not_wired(
    application_facade,
):
    """``smart_query_orchestrator`` is optional in
    ``create_rest_api`` — without it the endpoint must explicitly
    fail with 503 + a wiring hint."""
    app = create_rest_api(application_facade)
    client = TestClient(app)
    resp = client.post(
        "/dev/query-trace",
        json={"question": "anything"},
        headers={"X-Tenant-Id": "acme", "X-Project-Id": "alpha"},
    )
    assert resp.status_code == 503
    assert "smart_query_orchestrator" in resp.text


# ---- Happy path ------------------------------------------------


def test_returns_trace_payload_for_question(
    application_facade,
):
    orch = _good_orchestrator()
    app = create_rest_api(
        application_facade,
        smart_query_orchestrator=orch,
    )
    client = TestClient(app)
    resp = client.post(
        "/dev/query-trace",
        json={"question": _FAILED_QUERY, "run_id": "run-1"},
        headers={"X-Tenant-Id": "acme", "X-Project-Id": "alpha"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body.get("data") or body  # envelope wraps
    assert data["final_status"] == "passed"
    assert "60%" in data["answer"]
    trace = data["trace"]
    # Trace carries the plan + routes + candidates + gates.
    assert trace["plan"]["intent"] == "stage_progression"
    assert "60%" in trace["plan"]["anchors"]
    assert len(trace["routes_executed"]) >= 1
    assert "60%" in trace["groups_covered"]
    assert trace["final_status"] == "passed"


def test_rejects_empty_question(application_facade):
    orch = _good_orchestrator()
    app = create_rest_api(
        application_facade, smart_query_orchestrator=orch,
    )
    client = TestClient(app)
    resp = client.post(
        "/dev/query-trace",
        json={"question": ""},
        headers={"X-Tenant-Id": "acme", "X-Project-Id": "alpha"},
    )
    assert resp.status_code == 400


def test_evidence_insufficient_path_does_not_call_llm(
    application_facade,
):
    """When the sufficiency gate fails, the endpoint returns the
    trace with ``final_status=evidence_insufficient`` and the trace
    shows zero llm_evidence + gate failure reasons."""
    sparse_routes = {
        RetrievalRouteKind.RAGANYTHING: _DictRoute(
            RetrievalRouteKind.RAGANYTHING,
            {"primary": [_cand(artifact_id="X",
                               body="60% design only.")]},
        ),
    }
    llm_called: list[int] = []

    def _llm(req):
        llm_called.append(1)
        return "x"
    orch = SmartQueryOrchestrator.from_components(
        routes=sparse_routes, llm=_llm,
    )
    app = create_rest_api(
        application_facade, smart_query_orchestrator=orch,
    )
    client = TestClient(app)
    resp = client.post(
        "/dev/query-trace",
        json={"question": _FAILED_QUERY, "run_id": "run-1"},
        headers={"X-Tenant-Id": "acme", "X-Project-Id": "alpha"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    data = body.get("data") or body
    assert data["final_status"] == "evidence_insufficient"
    # LLM was never called.
    assert llm_called == []
    # Trace shows the missing groups.
    trace = data["trace"]
    missing = set(trace["groups_missing"])
    assert "90%" in missing
    assert "100% design" in missing
