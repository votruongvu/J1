"""Observability helpers for ingestion runs.

Distinct from ``j1.audit`` (business events) and ``j1.logging`` (the
process-wide stderr logger). This package owns developer / operator
trace surfaces that are noisy by design and disabled in production.
"""
