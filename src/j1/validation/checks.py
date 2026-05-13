"""Deprecated — this module's deterministic check engine has been
removed.

The validation pipeline now flows through
``SmartQueryOrchestrator`` (see ``j1.query.orchestrator``). The
orchestrator's ``AnswerQualityGate`` owns refusal detection,
evidence sufficiency, and citation binding via explicit,
individually-testable gates. The legacy ``run_checks`` +
``aggregate_status`` rule that let multi-paragraph refusal answers
mark "Passed" no longer exists.

What survived this deletion:

  * ``_is_abstain_response`` moved to ``j1.validation.runner`` (its
    only remaining caller — the negative-case check).
  * Case-specific ``_check_expected_*`` helpers moved into
    ``j1.validation.runner`` (they layer on top of the orchestrator
    output).

This file is kept as an explicit tombstone so anyone touching old
``from j1.validation.checks import ...`` lines gets a clear
``ImportError`` pointing them at the new home.
"""

from __future__ import annotations


__all__: list[str] = []
