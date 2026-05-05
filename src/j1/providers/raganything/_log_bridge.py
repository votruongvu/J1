"""Bridge raganything / MinerU log output into a `ProgressReporter`.

MinerU emits progress to its `loguru` / `logging` handlers as
human-readable lines. This module installs a `logging.Handler` that
parses those lines via `MinerUProgressParser` and pushes structured
events into a `ProgressReporter`.

Use as a context manager around the raganything call:

    with attach_mineru_progress_handler(reporter, ctx, run_id):
        await rag.process_document_complete(...)

Side-effect free outside the `with` block — the handler is removed
on exit, so no stray output gets captured by other code paths."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from j1.projects.context import ProjectContext
from j1.providers.raganything._progress import MinerUProgressParser
from j1.runs.reporter import ProgressReporter

__all__ = ["attach_mineru_progress_handler"]


# Logger names that mineru / raganything use. We attach to the parent
# logger so child loggers inherit the handler. Tested experimentally
# against mineru 3.x — both `mineru` and `raganything` emit through
# the standard logging hierarchy.
_MINERU_LOGGER_NAMES = ("mineru", "raganything")


class _ProgressLoggingHandler(logging.Handler):
    """logging.Handler that pipes formatted records through the
    MinerU parser and reports the resulting events.

    Failure modes are deliberately silent: parsing errors / reporter
    exceptions don't propagate (we don't want telemetry to break
    ingestion). Throttling lives downstream in
    `AuditProgressReporter` (5% delta threshold)."""

    def __init__(
        self,
        reporter: ProgressReporter,
        ctx: ProjectContext,
        run_id: str,
    ) -> None:
        super().__init__()
        self._reporter = reporter
        self._ctx = ctx
        self._run_id = run_id
        self._parser = MinerUProgressParser()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = record.getMessage()
        except Exception:  # noqa: BLE001
            return
        try:
            event = self._parser.feed_one(line)
        except Exception:  # noqa: BLE001
            return
        if event is None:
            return
        # Map MinerUProgressEvent → ProgressReporter call.
        try:
            if event.event_type == "step.progress":
                self._reporter.report_step_progress(
                    self._ctx,
                    run_id=self._run_id,
                    stage=event.stage,
                    step=event.step,
                    progress_percent=event.progress_percent or 0,
                    current=event.current,
                    total=event.total,
                    message=event.message,
                    engine=event.engine,
                )
            elif event.event_type == "step.completed":
                self._reporter.report_step_completed(
                    self._ctx,
                    run_id=self._run_id,
                    stage=event.stage,
                    step=event.step,
                )
        except Exception:  # noqa: BLE001
            # Telemetry must never block the underlying mineru run.
            return


@contextmanager
def attach_mineru_progress_handler(
    reporter: ProgressReporter | None,
    ctx: ProjectContext,
    run_id: str,
) -> Iterator[None]:
    """Attach a MinerU-aware progress handler to the mineru +
    raganything loggers for the duration of the `with` block.

    `reporter=None` (default in tests / dry runs) is a no-op — we
    don't want the test suite to install a global logging handler.

    The handler attaches to the named loggers' `addHandler()` rather
    than the root logger to avoid capturing unrelated log output in
    deployments that share the root configuration."""
    if reporter is None or not run_id:
        # No-op when EITHER the reporter is missing OR the run_id is
        # empty. Both are required to correlate progress events with
        # a run; without one of them the handler has nowhere to send
        # the parsed events.
        yield
        return
    handler = _ProgressLoggingHandler(reporter, ctx, run_id)
    handler.setLevel(logging.DEBUG)
    attached: list[tuple[logging.Logger, int]] = []
    try:
        for name in _MINERU_LOGGER_NAMES:
            logger = logging.getLogger(name)
            # Capture the existing effective level so we can restore
            # it on exit. mineru / raganything tend to default to
            # INFO, but if the deployment has globally raised the
            # level we need to lower it temporarily so progress lines
            # reach our handler.
            previous_level = logger.level
            if logger.level == logging.NOTSET or logger.level > logging.INFO:
                logger.setLevel(logging.INFO)
            logger.addHandler(handler)
            attached.append((logger, previous_level))
        yield
    finally:
        for logger, previous_level in attached:
            try:
                logger.removeHandler(handler)
            except ValueError:
                pass
            logger.setLevel(previous_level)
