from enum import StrEnum

CONFIDENCE_HIGH_THRESHOLD = 0.8
CONFIDENCE_MEDIUM_THRESHOLD = 0.5
CONFIDENCE_LOW_THRESHOLD = 0.2


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    AMBIGUOUS = "ambiguous"


def confidence_level_from_score(score: float) -> ConfidenceLevel:
    if score >= CONFIDENCE_HIGH_THRESHOLD:
        return ConfidenceLevel.HIGH
    if score >= CONFIDENCE_MEDIUM_THRESHOLD:
        return ConfidenceLevel.MEDIUM
    if score >= CONFIDENCE_LOW_THRESHOLD:
        return ConfidenceLevel.LOW
    return ConfidenceLevel.AMBIGUOUS


class WarningCategory(StrEnum):
    INFORMATIONAL = "informational"
    REVIEW_REQUIRED = "review_required"
    HIGH_RISK = "high_risk"
    SOURCE_VERIFICATION_REQUIRED = "source_verification_required"
    NOT_FOR_FINAL_DECISION = "not_for_final_decision"
