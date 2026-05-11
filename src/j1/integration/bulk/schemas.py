"""Bulk import/export record schemas.

Pydantic v2 models with camelCase JSON aliases — same convention as the
REST adapter's wire format. These schemas are the **public contract** of
the bulk API; they intentionally do not expose framework-internal types
(no `ProcessingStatus` enum, no `ProjectContext` dataclass) so callers
can serialise/deserialise without depending on `j1.*` modules.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _RecordModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",  # tolerate extra fields on import (forward-compat)
    )


# ---- Shared ---------------------------------------------------------


class TenantScope(_RecordModel):
    """Per-record tenant + project — verified against the request scope."""
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)


# ---- Documents / sources --------------------------------------------


class DocumentExportRecord(_RecordModel):
    document_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    original_filename: str
    stored_filename: str
    mime_type: str | None = None
    file_size: int = Field(ge=0)
    checksum: str = Field(min_length=1)
    status: str
    created_at: datetime


# `sources.ndjson` is structurally identical to `documents.ndjson`. We
# keep a separate type so OpenAPI documents the alias clearly.
class SourceExportRecord(DocumentExportRecord):
    """Alias of `DocumentExportRecord` — sources == documents in J1."""


# ---- Artifacts (the spec's "chunks") --------------------------------


class ArtifactExportRecord(_RecordModel):
    artifact_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    kind: str
    location: str  # workspace-relative, never absolute
    content_hash: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    status: str
    review_status: str
    version: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime
    source_document_ids: list[str] = Field(default_factory=list)
    source_artifact_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ---- Citations (export-only — derived from artifact lineage) --------


class CitationExportRecord(_RecordModel):
    artifact_id: str
    artifact_type: str
    source_document_id: str
    source_location: str | None = None


# ---- Metadata (denormalised projection of document fields) ----------


class MetadataExportRecord(_RecordModel):
    """Denormalised, analytics-friendly projection of `DocumentExportRecord`.

 Imported back, this format is used as a round-trip integrity check:
 it must reference an existing `documentId` and the supplied fields
 must match the registry's stored values. Useful for verifying a
 backup/restore cycle.
 """
    document_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    original_filename: str
    mime_type: str | None = None
    file_size: int = Field(ge=0)
    checksum: str = Field(min_length=1)
    status: str
    created_at: datetime


# ---- Feedback (export-only — append-only audit data) ----------------


class FeedbackExportRecord(_RecordModel):
    feedback_id: str
    tenant_id: str
    project_id: str
    target_kind: str
    target_id: str
    submitted_at: datetime
    rating: int | None = None
    comment: str | None = None
    actor: str | None = None
    correlation_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
