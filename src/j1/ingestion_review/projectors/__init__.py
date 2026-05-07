"""Projection layer for the ingestion-review service.

Each projector converts producer-specific artifact content into the
neutral DTOs the REST surface returns. Projectors stay vendor-blind
— LightRAG / RAGAnything / mineru fields are translated here, and
the DTOs above the projector layer never see them.
"""

from j1.ingestion_review.projectors.chunks import ChunkProjector
from j1.ingestion_review.projectors.graph import GraphSnapshotProjector
from j1.ingestion_review.projectors.quality import QualityReportProjector

__all__ = ["ChunkProjector", "GraphSnapshotProjector", "QualityReportProjector"]
