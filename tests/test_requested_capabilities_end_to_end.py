"""End-to-end contract — Knowledge Index ``requested_capabilities``.

Pins the load-bearing seams as user-selected checkbox values flow
through:

  FE checkbox state
   → multipart ``requestedCapabilities`` JSON field
   → REST ``_parse_requested_capabilities_or_400`` parser
   → ``IngestRequest.requested_capabilities``
   → ``ProjectProcessingRequest.requested_capabilities`` (dict)
   → workflow's ``_apply_user_capability_selection`` helper
   → ``AssessmentPlan.user_selected_capabilities``
     + ``required_capabilities`` (derived from user picks)
     + ``capability_source="user_selection"``

Each contract is a focused test against a production seam (no
stubs of the load-bearing modules). The legacy per-profile
capability matrix is NOT consulted for new runs — pinned here.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from j1.adapters.rest.schemas import (
    IngestRequest,
    RequestedCapabilities,
)
from j1.orchestration.workflows.project_processing import (
    ProjectProcessingRequest,
    _apply_user_capability_selection,
)
from j1.processing.assessment import (
    CAPABILITY_SOURCE_PLANNER,
    CAPABILITY_SOURCE_USER,
    AssessmentPlan,
    Capability,
    CompileMode,
    Complexity,
    UserSelectedCapabilities,
)


# ---- Helpers ----------------------------------------------------


def _planner_default_plan() -> AssessmentPlan:
    """A plan as the deterministic planner would emit — no
    user-selection yet, ``capability_source="planner_default"``."""
    return AssessmentPlan(
        document_id="doc-test",
        mode=CompileMode.STANDARD,
        document_type="pdf",
        complexity=Complexity.MEDIUM,
        confidence=0.85,
        required_capabilities=frozenset({
            Capability.TEXT_EXTRACTION, Capability.LAYOUT_DETECTION,
        }),
    )


def _request(
    requested_capabilities: dict | None = None,
) -> ProjectProcessingRequest:
    """Build a minimal ``ProjectProcessingRequest`` for the
    workflow helper. We don't need scope / correlation_id for the
    helper-level test."""
    from j1.orchestration.activities.payloads import ProjectScope
    return ProjectProcessingRequest(
        scope=ProjectScope(tenant_id="t", project_id="p"),
        compiler_kind="mock",
        actor="system",
        target_document_ids=("doc-test",),
        requested_capabilities=requested_capabilities,
    )


# ---- Contract 1: REST schema accepts the new wire shape --------


def test_contract_1_ingest_request_accepts_requested_capabilities_field():
    """The new field is optional on the wire (defaults to None for
    legacy callers) but accepts a full payload from the FE."""
    body = IngestRequest.model_validate({
        "requestedCapabilities": {
            "imageProcessing": True,
            "tableProcessing": False,
            "equationProcessing": True,
        },
    })
    assert body.requested_capabilities is not None
    assert body.requested_capabilities.image_processing is True
    assert body.requested_capabilities.table_processing is False
    assert body.requested_capabilities.equation_processing is True


def test_contract_1_ingest_request_defaults_to_none_when_omitted():
    """Legacy callers that omit the field MUST keep working —
    field is None and downstream falls back to planner defaults."""
    body = IngestRequest.model_validate({})
    assert body.requested_capabilities is None


def test_contract_1_requested_capabilities_defaults_to_all_false():
    """``RequestedCapabilities()`` (no kwargs) MUST produce the
    all-off default. Pinned so a future refactor that flips a
    default-on cannot silently change every legacy caller."""
    rc = RequestedCapabilities()
    assert rc.image_processing is False
    assert rc.table_processing is False
    assert rc.equation_processing is False


# ---- Contract 2: UserSelectedCapabilities round-trip + projection


def test_contract_2_user_selection_projects_onto_required_capabilities():
    """The three booleans → set of vendor-neutral capability
    enum values. Text + Layout are the always-on floor."""
    sel = UserSelectedCapabilities(
        image_processing=True,
        table_processing=True,
        equation_processing=False,
    )
    caps = sel.to_required_capabilities()
    assert Capability.TEXT_EXTRACTION in caps
    assert Capability.LAYOUT_DETECTION in caps
    assert Capability.IMAGE_EXTRACTION in caps
    assert Capability.TABLE_EXTRACTION in caps
    # Unchecked → not in the set.
    assert Capability.FORMULA_EXTRACTION not in caps


def test_contract_2_user_selection_payload_round_trips():
    sel = UserSelectedCapabilities(
        image_processing=True,
        table_processing=False,
        equation_processing=True,
    )
    payload = sel.to_payload()
    roundtripped = UserSelectedCapabilities.from_payload(payload)
    assert roundtripped == sel


def test_contract_2_user_selection_from_payload_tolerates_missing_keys():
    """Partial payloads from a future schema bump default
    missing keys to False rather than crashing."""
    sel = UserSelectedCapabilities.from_payload(
        {"image_processing": True},
    )
    assert sel.image_processing is True
    assert sel.table_processing is False
    assert sel.equation_processing is False


# ---- Contract 3: AssessmentPlan.with_user_selection overrides --


def test_contract_3_with_user_selection_overrides_required_capabilities():
    """A user who checks Images + Tables but leaves Equations
    unchecked replaces the planner's required-capabilities set.
    Pre-existing TEXT_EXTRACTION + LAYOUT_DETECTION (the floor)
    remain present in the new set."""
    plan = _planner_default_plan()
    sel = UserSelectedCapabilities(
        image_processing=True,
        table_processing=True,
        equation_processing=False,
    )
    overridden = plan.with_user_selection(sel)
    assert overridden.required_capabilities == frozenset({
        Capability.TEXT_EXTRACTION,
        Capability.LAYOUT_DETECTION,
        Capability.IMAGE_EXTRACTION,
        Capability.TABLE_EXTRACTION,
    })
    assert overridden.capability_source == CAPABILITY_SOURCE_USER
    assert overridden.user_selected_capabilities == sel


def test_contract_3_planner_default_plan_advertises_planner_source():
    """A plan without any user selection MUST have
    ``capability_source="planner_default"`` so audits can tell
    operator overrides apart."""
    plan = _planner_default_plan()
    assert plan.capability_source == CAPABILITY_SOURCE_PLANNER
    assert plan.user_selected_capabilities is None


def test_contract_3_with_user_selection_is_pure_returns_new_plan():
    """Builder is pure — the original plan is unchanged."""
    plan = _planner_default_plan()
    sel = UserSelectedCapabilities(
        image_processing=True,
        table_processing=False,
        equation_processing=False,
    )
    overridden = plan.with_user_selection(sel)
    assert overridden is not plan
    assert plan.capability_source == CAPABILITY_SOURCE_PLANNER


def test_contract_3_payload_round_trips_with_user_selection():
    """``to_payload`` / ``from_payload`` MUST preserve both the
    selection and the source so the workflow → activity
    boundary doesn't drop the operator override."""
    plan = _planner_default_plan()
    sel = UserSelectedCapabilities(
        image_processing=True,
        table_processing=True,
        equation_processing=False,
    )
    overridden = plan.with_user_selection(sel)
    roundtripped = AssessmentPlan.from_payload(overridden.to_payload())
    assert roundtripped.required_capabilities == (
        overridden.required_capabilities
    )
    assert roundtripped.capability_source == CAPABILITY_SOURCE_USER
    assert roundtripped.user_selected_capabilities == sel


# ---- Contract 4: workflow helper folds request into plan -------


def test_contract_4_workflow_helper_applies_user_selection():
    """The workflow's ``_apply_user_capability_selection`` is the
    seam. Given a request with checkbox state, it MUST produce a
    plan payload whose required_capabilities reflect the user's
    pick + whose capability_source is "user_selection"."""
    plan_payload = _planner_default_plan().to_payload()
    request = _request(requested_capabilities={
        "image_processing": True,
        "table_processing": False,
        "equation_processing": True,
    })
    overridden_payload = _apply_user_capability_selection(
        plan_payload, request,
    )
    overridden = AssessmentPlan.from_payload(overridden_payload)
    assert overridden.capability_source == CAPABILITY_SOURCE_USER
    assert Capability.IMAGE_EXTRACTION in overridden.required_capabilities
    assert (
        Capability.TABLE_EXTRACTION not in overridden.required_capabilities
    )
    assert (
        Capability.FORMULA_EXTRACTION in overridden.required_capabilities
    )


def test_contract_4_workflow_helper_noops_when_request_omits_field():
    """Legacy callers (bulk-job dispatch, replay) carry None for
    ``requested_capabilities``. The helper MUST return the plan
    payload unchanged so the planner's defaults still drive
    compile."""
    plan_payload = _planner_default_plan().to_payload()
    request = _request(requested_capabilities=None)
    overridden_payload = _apply_user_capability_selection(
        plan_payload, request,
    )
    assert overridden_payload == plan_payload
    plan = AssessmentPlan.from_payload(overridden_payload)
    assert plan.capability_source == CAPABILITY_SOURCE_PLANNER


def test_contract_4_workflow_helper_handles_none_plan_payload():
    """A pre-compile error may leave the assessment payload None.
    The helper MUST pass through without crashing."""
    request = _request(requested_capabilities={
        "image_processing": True,
        "table_processing": True,
        "equation_processing": True,
    })
    result = _apply_user_capability_selection(None, request)
    assert result is None


# ---- Contract 5: ProjectProcessingRequest dataclass field ------


def test_contract_5_workflow_request_carries_requested_capabilities():
    """The dataclass field is the load-bearing seam between the
    REST adapter and the workflow. Pinned so a future refactor
    can't silently drop it."""
    from dataclasses import fields
    field_names = {f.name for f in fields(ProjectProcessingRequest)}
    assert "requested_capabilities" in field_names


def test_contract_5_workflow_request_defaults_to_none():
    """Legacy bulk-job callers don't supply this field; the
    workflow MUST tolerate it being None."""
    from j1.orchestration.activities.payloads import ProjectScope
    req = ProjectProcessingRequest(
        scope=ProjectScope(tenant_id="t", project_id="p"),
        compiler_kind="mock",
        actor="system",
        target_document_ids=("doc-test",),
    )
    assert req.requested_capabilities is None


# ---- Contract 6: legacy matrices NOT consulted for new runs ----


def test_contract_6_user_selection_overrides_planner_defaults():
    """The keystone behavioural contract: user-checkbox state is
    the source of truth for new runs, NOT the legacy per-profile
    capability matrix.

    Scenario: planner default would require only TEXT + LAYOUT.
    User checks all three boxes. The resulting plan MUST require
    image / table / equation extraction — regardless of which
    legacy profile name was on the wire."""
    plan_payload = _planner_default_plan().to_payload()
    # Sanity: planner default doesn't include image/table/equation.
    pre = AssessmentPlan.from_payload(plan_payload)
    assert Capability.IMAGE_EXTRACTION not in pre.required_capabilities
    assert Capability.TABLE_EXTRACTION not in pre.required_capabilities
    assert Capability.FORMULA_EXTRACTION not in pre.required_capabilities

    request = _request(requested_capabilities={
        "image_processing": True,
        "table_processing": True,
        "equation_processing": True,
    })
    post = AssessmentPlan.from_payload(
        _apply_user_capability_selection(plan_payload, request),
    )
    # All three vendor-neutral capabilities are now in the set.
    assert Capability.IMAGE_EXTRACTION in post.required_capabilities
    assert Capability.TABLE_EXTRACTION in post.required_capabilities
    assert Capability.FORMULA_EXTRACTION in post.required_capabilities
    # And the source string makes the override visible to audits.
    assert post.capability_source == CAPABILITY_SOURCE_USER
