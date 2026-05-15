"""Wire vocabulary for the post-index Manual Actions surface.

The default Index path keeps things lightweight: assessment → rule
matching → user-selected profile → RAGAnything compile → basic
searchable / evidence index → final report. Anything heavier is an
EXPLICIT operator action exposed under
``GET /documents/{id}/manual-actions`` so the FE can render buttons
with status, cost expectation, and a clear trigger.

This module is intentionally declarative — it defines the action
vocabulary + a small status helper. The actual implementations of
domain enrichment / knowledge memory / entity normalization / deep
knowledge index live in their own modules (or are deferred to a
future change). The FE renders a button per registered action and
the corresponding ``POST /documents/{id}/manual-actions/{action_id}``
endpoint either dispatches the action or returns 501 until the
implementation lands.

Why a declared vocabulary instead of a free-form list:

  * The FE renders source-aware copy keyed on the action id — a
    rename here would silently break the dialog without the
    vocabulary pin.
  * The recommendation resolver references the same ids when the
    LLM Advanced Assessment recommends "next steps" (e.g.
    ``run_domain_enrichment``).
  * Tests pin the set so a new optional action doesn't ship without
    a matching FE handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


__all__ = [
    "ACTION_RUN_LLM_ADVANCED_ASSESSMENT",
    "ACTION_RUN_DOMAIN_ENRICHMENT",
    "ACTION_BUILD_KNOWLEDGE_MEMORY",
    "ACTION_NORMALIZE_ENTITIES",
    "ACTION_BUILD_DEEP_KNOWLEDGE_INDEX",
    "ACTION_RUN_MULTIMODAL_ENRICHMENT",
    "ManualActionDescriptor",
    "list_manual_actions",
    "MANUAL_ACTIONS",
    "MANUAL_ACTION_STATUS_AVAILABLE",
    "MANUAL_ACTION_STATUS_NOT_IMPLEMENTED",
    "MANUAL_ACTION_STATUS_DISABLED",
]


# Wire identifiers. Stable strings — renames require a coordinated
# FE + BE change. Keep in sync with the LLM Advanced Assessment's
# ``_ALLOWED_NEXT_STEPS`` set in
# :mod:`j1.processing.llm_advanced_assessment`.

ACTION_RUN_LLM_ADVANCED_ASSESSMENT = "run_llm_advanced_assessment"
ACTION_RUN_DOMAIN_ENRICHMENT = "run_domain_enrichment"
ACTION_BUILD_KNOWLEDGE_MEMORY = "build_knowledge_memory"
ACTION_NORMALIZE_ENTITIES = "normalize_entities"
ACTION_BUILD_DEEP_KNOWLEDGE_INDEX = "build_deep_knowledge_index"
ACTION_RUN_MULTIMODAL_ENRICHMENT = "run_multimodal_enrichment"


# ---- Status vocabulary --------------------------------------------

MANUAL_ACTION_STATUS_AVAILABLE = "available"
MANUAL_ACTION_STATUS_NOT_IMPLEMENTED = "not_implemented"
MANUAL_ACTION_STATUS_DISABLED = "disabled"


@dataclass(frozen=True)
class ManualActionDescriptor:
    """One advertised manual action.

    ``id`` is the wire id; ``label`` / ``description`` /
    ``cost_note`` drive the FE button + tooltip; ``status`` tells
    the FE whether to enable the button:

      ``available``        — the corresponding endpoint will
                             dispatch the action.
      ``not_implemented``  — endpoint returns 501; the button
                             renders disabled with a "coming
                             soon" tooltip.
      ``disabled``         — the deployment turned this action
                             off via env policy.
    """

    id: str
    label: str
    description: str
    cost_note: str
    status: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "costNote": self.cost_note,
            "status": self.status,
        }


# Canonical set. Order is render-order in the FE (top to bottom).
MANUAL_ACTIONS: tuple[ManualActionDescriptor, ...] = (
    ManualActionDescriptor(
        id=ACTION_RUN_LLM_ADVANCED_ASSESSMENT,
        label="Run Advanced Assessment",
        description=(
            "Use an LLM to estimate document complexity and "
            "recommend a profile + downstream manual actions. "
            "Reads sampled text only — never uploads the full "
            "file unless your deployment explicitly enables it."
        ),
        cost_note=(
            "Uses one LLM call. May take 10–60 seconds. May incur "
            "provider cost."
        ),
        # Available by default — the LLM-disabled deployment path is
        # surfaced at the SERVICE level (returns a structured
        # refusal), not at the action-list level, so the operator
        # can see WHY it refused.
        status=MANUAL_ACTION_STATUS_AVAILABLE,
    ),
    ManualActionDescriptor(
        id=ACTION_RUN_DOMAIN_ENRICHMENT,
        label="Run Domain Enrichment",
        description=(
            "Apply the active domain pack's per-enricher prompts "
            "(tables, images, metadata, classification, "
            "validation) over the already-compiled artifacts."
        ),
        cost_note=(
            "Multiple LLM calls, one per enricher. Cost scales "
            "with document length."
        ),
        status=MANUAL_ACTION_STATUS_NOT_IMPLEMENTED,
    ),
    ManualActionDescriptor(
        id=ACTION_BUILD_KNOWLEDGE_MEMORY,
        label="Build Knowledge Memory",
        description=(
            "Persist a structured knowledge memory layer on top of "
            "the compiled chunks (entities, relationships, "
            "summary cache)."
        ),
        cost_note=(
            "LLM-heavy. Suitable for documents that will be "
            "queried frequently."
        ),
        status=MANUAL_ACTION_STATUS_NOT_IMPLEMENTED,
    ),
    ManualActionDescriptor(
        id=ACTION_NORMALIZE_ENTITIES,
        label="Normalize Entities",
        description=(
            "Cluster extracted entity mentions across the document "
            "into canonical entities (cross-reference IDs, names, "
            "aliases)."
        ),
        cost_note=(
            "LLM + deterministic matching. Cost scales with entity "
            "count."
        ),
        status=MANUAL_ACTION_STATUS_NOT_IMPLEMENTED,
    ),
    ManualActionDescriptor(
        id=ACTION_BUILD_DEEP_KNOWLEDGE_INDEX,
        label="Build Deep Knowledge Index",
        description=(
            "Extend the base index with hierarchical chunking, "
            "graph-aware embeddings, and entity-bound retrieval."
        ),
        cost_note=(
            "Embedding + LLM. Storage doubles vs the basic index."
        ),
        status=MANUAL_ACTION_STATUS_NOT_IMPLEMENTED,
    ),
    ManualActionDescriptor(
        id=ACTION_RUN_MULTIMODAL_ENRICHMENT,
        label="Run Multimodal Enrichment",
        description=(
            "Send images / tables / diagrams to a vision LLM for "
            "structured description. Skip when the document has "
            "no visual content."
        ),
        cost_note=(
            "Vision LLM calls. Higher per-call cost than text-only."
        ),
        status=MANUAL_ACTION_STATUS_NOT_IMPLEMENTED,
    ),
)


_BY_ID = {a.id: a for a in MANUAL_ACTIONS}


def list_manual_actions() -> tuple[ManualActionDescriptor, ...]:
    """Return the canonical action set."""
    return MANUAL_ACTIONS


def get_manual_action(action_id: str) -> ManualActionDescriptor | None:
    """Lookup helper. Returns ``None`` for unknown ids so the REST
    handler can 404 cleanly."""
    return _BY_ID.get(action_id)
