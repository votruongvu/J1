"""artifact endpoint contract.

The new pipeline exposes three typed-artifact endpoints alongside
the legacy compile/enrich plan endpoints:

 * `GET /ingestion-runs/{run_id}/initial-execution-plan`
 * `GET /ingestion-runs/{run_id}/compile-result`
 * `GET /ingestion-runs/{run_id}/enrichment-result`

The FE consumes all three with one fetch shape. This test file pins
the cross-endpoint wire contract so a refactor that changes one
endpoint's keys (or its "unavailable" sentinel structure) is flagged
at CI before the FE breaks.

Each endpoint's response (under the API envelope's `data` field) is
required to carry the SAME 7 keys:

 runId — string
 documentId — string | None
 documentName — string | None
 status — "completed" | "unavailable"
 unavailableReason — string | None
 artifactId — string | absent (only when status="completed")
 plan — dict | None

This is the contract — adding extra fields is allowed; renaming or
dropping any of the 7 is a coordinated FE change.
"""

from __future__ import annotations

import inspect

import pytest

from j1.ingestion_review.service import IngestionResultReviewService


# ---- 1. Contract surface --------------------------------------------


_CONTRACT_METHODS = (
    "get_run_initial_execution_plan",
    "get_run_compile_result",
    "get_run_enrichment_result",
)


_REQUIRED_KEYS = frozenset({
    "runId", "documentId", "documentName",
    "status", "unavailableReason", "plan",
})


@pytest.mark.parametrize("method_name", _CONTRACT_METHODS)
def test_service_method_exists_and_is_callable(method_name):
    """Each of the three contract methods must be a public attribute
 on the service. Renames are FE-coordinated."""
    assert hasattr(IngestionResultReviewService, method_name), (
        f"{method_name} missing on IngestionResultReviewService — this is "
        f"a wire-breaking rename"
    )
    method = getattr(IngestionResultReviewService, method_name)
    assert callable(method)


@pytest.mark.parametrize("method_name", _CONTRACT_METHODS)
def test_service_method_signature_matches_contract(method_name):
    """Every contract method takes exactly `(self, ctx, run_id)` —
 the FE depends on the (run_id) routing parameter being the only
 request-shaped input. Optional kwargs are allowed but must default
 so existing callers don't break."""
    method = getattr(IngestionResultReviewService, method_name)
    sig = inspect.signature(method)
    params = list(sig.parameters.values())
    # self + ctx + run_id
    positional_required = [
        p for p in params
        if p.default is inspect.Parameter.empty
        and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
    ]
    assert len(positional_required) == 3, (
        f"{method_name} signature has changed: required positional "
        f"params = {[p.name for p in positional_required]}"
    )
    assert positional_required[1].name == "ctx"
    assert positional_required[2].name == "run_id"


# ---- 2. Unavailable-payload shape (no I/O — direct construction) ----


def _unavailable_payload(reason: str = "no artifact yet") -> dict:
    """A reference unavailable payload matching what the service
 methods return when no artifact has been persisted yet. This is
 the wire contract — if a method returns something with different
 keys, the FE breaks."""
    return {
        "runId": "run-xyz",
        "documentId": None,
        "documentName": None,
        "status": "unavailable",
        "unavailableReason": reason,
        "plan": None,
    }


def test_unavailable_payload_has_all_required_keys():
    payload = _unavailable_payload()
    assert _REQUIRED_KEYS <= set(payload.keys()), (
        f"unavailable payload missing keys: "
        f"{_REQUIRED_KEYS - set(payload.keys())}"
    )


def test_unavailable_payload_status_is_exactly_the_string_unavailable():
    """The FE branches on exact string equality. Don't accept
 `"UNAVAILABLE"` or `null` — they break the FE consumer."""
    payload = _unavailable_payload()
    assert payload["status"] == "unavailable"


def test_unavailable_payload_carries_an_operator_readable_reason():
    """The FE renders the `unavailableReason` string directly in the
 panel. Empty / null reasons leave the operator without context —
 the contract requires a non-empty string when status=unavailable."""
    payload = _unavailable_payload("the run hasn't reached pre-compile")
    assert payload["unavailableReason"]
    assert isinstance(payload["unavailableReason"], str)


def test_completed_payload_carries_artifact_id_and_plan():
    """When status=completed, the contract MUST surface `artifactId`
 so the FE can deep-link to the underlying artifact view, plus
 the typed `plan` payload the FE renders."""
    completed = {
        "runId": "run-xyz",
        "documentId": "doc-1",
        "documentName": "spec.pdf",
        "status": "completed",
        "unavailableReason": None,
        "artifactId": "art-123",
        "plan": {"some": "payload"},
    }
    assert completed["status"] == "completed"
    assert completed["artifactId"]
    assert isinstance(completed["plan"], dict)


# ---- 3. Docstring contract pinning ---------------------------------


@pytest.mark.parametrize("method_name", _CONTRACT_METHODS)
def test_service_method_documents_unavailable_status(method_name):
    """Each contract method's docstring must reference the
 `status="unavailable"` sentinel so a future maintainer doesn't
 silently drop the sentinel from the response."""
    method = getattr(IngestionResultReviewService, method_name)
    doc = method.__doc__ or ""
    assert "unavailable" in doc.lower(), (
        f"{method_name} docstring must mention the unavailable status; "
        f"saw:\n{doc[:300]}"
    )


# ---- 4. REST adapter wire-up ---------------------------------------


def test_rest_app_exposes_all_three_endpoints():
    """The REST adapter mounts handlers for each contract method.
 Renaming an endpoint route is a coordinated FE change — this
 pin catches accidental drift."""
    from j1.adapters.rest import app as rest_app
    src = inspect.getsource(rest_app)
    for route in (
        "/ingestion-runs/{run_id}/initial-execution-plan",
        "/ingestion-runs/{run_id}/compile-result",
        "/ingestion-runs/{run_id}/enrichment-result",
    ):
        assert route in src, f"REST app missing route {route!r}"


def test_rest_app_uses_envelope_helper_for_artifact_endpoints():
    """Every artifact endpoint passes its dict through `envelope(...)`
 so the FE sees the standard `{requestId, data, meta}` shape.
 Catches accidental raw-dict returns (which the FE consumer can't
 parse)."""
    from j1.adapters.rest import app as rest_app
    src = inspect.getsource(rest_app)
    # The three new methods are called from the adapter; their
    # invocation lines should hand the result to `envelope(...)`.
    for method_call in (
        "service.get_run_initial_execution_plan",
        "service.get_run_compile_result",
        "service.get_run_enrichment_result",
    ):
        idx = src.find(method_call)
        assert idx > 0, f"adapter does not call {method_call}"
        # Look ahead for envelope( within the same function body.
        nearby = src[idx : idx + 600]
        assert "envelope(" in nearby, (
            f"{method_call} result must pass through envelope() — "
            f"saw the call but no envelope() in the following 600 chars"
        )
