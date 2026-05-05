"""Tests for the MinerU log-line progress parser + log-bridge."""

from __future__ import annotations

import logging

import pytest

from j1.projects.context import ProjectContext
from j1.providers.raganything._log_bridge import attach_mineru_progress_handler
from j1.providers.raganything._progress import (
    MinerUProgressParser,
    parse_mineru_line,
)
from j1.runs import NoopProgressReporter


# ---- Single-line parsing ----------------------------------------


def test_parses_simple_layout_progress_line():
    """The basic format observed in dev runs:
    `[MinerU] Layout Preparation: 80% | 35/44`"""
    event = parse_mineru_line("[MinerU] Layout Preparation: 80% | 35/44")
    assert event is not None
    assert event.event_type == "step.progress"
    assert event.stage == "COMPILE"
    assert event.step == "LAYOUT_PREPARATION"
    assert event.engine == "MinerU"
    assert event.progress_percent == 80
    assert event.current == 35
    assert event.total == 44
    assert "35/44" in event.message


def test_parses_tqdm_layout_progress_line():
    """tqdm format (mineru emits this when running through loguru):
    `Layout Preparation: 100%|██████████| 1/1 [00:00<00:00, 43.03it/s]`"""
    event = parse_mineru_line(
        "Layout Preparation: 100%|██████████| 1/1 [00:00<00:00, 43.03it/s]"
    )
    assert event is not None
    assert event.progress_percent == 100
    assert event.current == 1
    assert event.total == 1


def test_parses_model_fetch_line():
    event = parse_mineru_line(
        "Fetching 13 files:   8%|▊         | 1/13 [00:00<00:03,  3.03it/s]"
    )
    assert event is not None
    assert event.step == "MODEL_FETCH"
    assert event.current == 1
    assert event.total == 13
    assert event.progress_percent == 8


def test_parses_predictor_loaded_as_step_completed():
    """`get transformers predictor cost: 50.12s` marks model-load
    completion. Surface as step.completed at 100%."""
    event = parse_mineru_line(
        "[MinerU] get transformers predictor cost: 50.12s"
    )
    assert event is not None
    assert event.event_type == "step.completed"
    assert event.step == "MODEL_LOAD"
    assert event.progress_percent == 100
    assert "50.12s" in (event.message or "")


def test_unrecognised_line_returns_none():
    assert parse_mineru_line("INFO: Multimodal processors initialized") is None
    assert parse_mineru_line("") is None
    assert parse_mineru_line("random log noise here") is None


def test_no_false_positive_on_mention_without_progress():
    """A line that mentions 'Layout' without a progress signature
    must NOT be parsed as progress."""
    event = parse_mineru_line("[MinerU] Layout module initialized")
    assert event is None


# ---- Streaming parser (deduplication) ---------------------------


def test_streaming_parser_deduplicates_repeated_progress_lines():
    """MinerU sometimes emits the SAME tqdm percentage twice in
    quick succession. The streaming parser drops duplicates so the
    reporter doesn't get spammed."""
    parser = MinerUProgressParser()
    events = list(parser.feed([
        "[MinerU] Layout Preparation: 25% | 11/44",
        "[MinerU] Layout Preparation: 25% | 11/44",
        "[MinerU] Layout Preparation: 50% | 22/44",
    ]))
    pcts = [e.progress_percent for e in events]
    assert pcts == [25, 50]


def test_streaming_parser_passes_completion_through_even_if_pct_matches():
    """A `step.completed` event must always pass through, even when
    the progress percent matches the last seen value."""
    parser = MinerUProgressParser()
    events = list(parser.feed([
        "[MinerU] Fetching 13 files: 100%|██████████| 13/13 [00:49<00:00]",
        "[MinerU] get transformers predictor cost: 50.12s",
    ]))
    assert len(events) == 2
    assert events[0].event_type == "step.progress"
    assert events[1].event_type == "step.completed"


# ---- Log bridge integration ------------------------------------


def test_attach_handler_no_op_when_reporter_is_none():
    """A `None` reporter must NOT install a global logging handler
    — important because deployments that opt out shouldn't have
    their logs intercepted."""
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    mineru_logger = logging.getLogger("mineru")
    handler_count_before = len(mineru_logger.handlers)
    with attach_mineru_progress_handler(None, ctx, "run-1"):
        assert len(mineru_logger.handlers) == handler_count_before
    assert len(mineru_logger.handlers) == handler_count_before


def test_attach_handler_routes_log_lines_into_reporter(monkeypatch):
    """End-to-end: when the handler is attached and mineru emits a
    progress line, the reporter receives a structured `step.progress`
    call. Without this integration the parser is dead code."""
    captured: list[dict] = []

    class _CapturingReporter(NoopProgressReporter):
        def report_step_progress(
            self, _ctx, *, run_id, stage, step,
            progress_percent, current=None, total=None,
            message=None, engine=None, actor="system",
        ):
            captured.append({
                "run_id": run_id, "stage": stage, "step": step,
                "progress_percent": progress_percent,
                "current": current, "total": total,
                "engine": engine,
            })
            return "evt-1"
        def report_step_completed(
            self, _ctx, *, run_id, stage, step,
            artifact_count=0, actor="system",
        ):
            captured.append({
                "run_id": run_id, "stage": stage, "step": step,
                "completed": True,
            })
            return "evt-done"

    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    reporter = _CapturingReporter()

    with attach_mineru_progress_handler(reporter, ctx, "run-42"):
        # Emit a progress line via the mineru logger — exactly how
        # the real mineru CLI emits its tqdm output.
        logging.getLogger("mineru").info(
            "[MinerU] Layout Preparation: 50% | 22/44"
        )
        logging.getLogger("mineru").info(
            "[MinerU] get transformers predictor cost: 50.12s"
        )

    progress_calls = [c for c in captured if "progress_percent" in c]
    completed_calls = [c for c in captured if c.get("completed")]

    assert len(progress_calls) == 1
    assert progress_calls[0]["run_id"] == "run-42"
    assert progress_calls[0]["stage"] == "COMPILE"
    assert progress_calls[0]["step"] == "LAYOUT_PREPARATION"
    assert progress_calls[0]["progress_percent"] == 50
    assert progress_calls[0]["engine"] == "MinerU"

    assert len(completed_calls) == 1
    assert completed_calls[0]["step"] == "MODEL_LOAD"


def test_handler_removed_on_context_exit():
    """The handler MUST be detached when the `with` block exits, so
    subsequent log activity in unrelated code paths doesn't get
    captured. This is a regression guard against handler leaks."""
    ctx = ProjectContext(tenant_id="acme", project_id="alpha")
    reporter = NoopProgressReporter()
    mineru_logger = logging.getLogger("mineru")
    handler_count_before = len(mineru_logger.handlers)
    with attach_mineru_progress_handler(reporter, ctx, "run-1"):
        assert len(mineru_logger.handlers) == handler_count_before + 1
    assert len(mineru_logger.handlers) == handler_count_before
