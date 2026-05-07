"""Ingestion-result review surface.

Read-only projection over completed ingestion runs. Distinct from
`j1.review` (the human-in-the-loop review queue): this package answers
"what did the ingestion produce, and how good is it?" — chunks,
assets, graph, quality report — for end-user review of completed runs.

Public entrypoint is `IngestionResultReviewService`. The REST adapter
wires it once at app construction and exposes `/ingestion-runs/{id}/*`
review endpoints.
"""

from j1.ingestion_review.dtos import (
    ArtifactPageDTO,
    ArtifactRecordDTO,
    AvailabilityDTO,
    AvailableViewsDTO,
    ChunkDetailDTO,
    ChunkPageDTO,
    ChunkPreviewDTO,
    FailedOptionalStepDTO,
    GraphEntityDTO,
    GraphRelationDTO,
    GraphSnapshotDTO,
    GraphStatsDTO,
    GraphTruncatedDTO,
    GraphTruncationLimitsDTO,
    GraphUnavailableDTO,
    LinkedAssetDTO,
    LowConfidenceFindingDTO,
    ModalityConfidenceDTO,
    QualityReportDTO,
    QualitySummaryDTO,
    RunSummaryDTO,
    SkippedStepDTO,
    StepErrorDTO,
    StepResultDTO,
    WarningDTO,
)
from j1.ingestion_review.exceptions import (
    ReviewNotFound,
    RunNotTerminal,
)
from j1.ingestion_review.service import (
    ArtifactContent,
    IngestionResultReviewService,
)

__all__ = [
    "ArtifactContent",
    "ArtifactPageDTO",
    "ArtifactRecordDTO",
    "AvailabilityDTO",
    "AvailableViewsDTO",
    "ChunkDetailDTO",
    "ChunkPageDTO",
    "ChunkPreviewDTO",
    "FailedOptionalStepDTO",
    "GraphEntityDTO",
    "GraphRelationDTO",
    "GraphSnapshotDTO",
    "GraphStatsDTO",
    "GraphTruncatedDTO",
    "GraphTruncationLimitsDTO",
    "GraphUnavailableDTO",
    "IngestionResultReviewService",
    "LinkedAssetDTO",
    "LowConfidenceFindingDTO",
    "ModalityConfidenceDTO",
    "QualityReportDTO",
    "QualitySummaryDTO",
    "ReviewNotFound",
    "RunNotTerminal",
    "RunSummaryDTO",
    "SkippedStepDTO",
    "StepErrorDTO",
    "StepResultDTO",
    "WarningDTO",
]
