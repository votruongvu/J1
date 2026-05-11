"""Parser for MinerU / RAGAnything progress lines.

MinerU (the document parser bundled with RAGAnything) emits progress
to its logger, not via callbacks. The lines look like:

 [MinerU] Layout Preparation: 80% | 35/44
 [MinerU] Fetching 13 files: 62%|██████▏ | 8/13 [00:49<00:31, 6.38s/it]
 [MinerU] get transformers predictor cost: 50.12s
 [MinerU] Hybrid processing-window run. page_count=1,...

To turn those into structured `step.progress` events for the frontend,
this module provides a `MinerUProgressParser` that:

 * Recognises the known progress shapes (regex over the formatted
 line, no module-internal coupling to mineru).
 * Returns a small structured dict per recognised line, or `None`
 for lines we don't care about.
 * Stays isolated inside the raganything provider — no parser-
 specific code leaks into workflow / activity / API code.

The parser is intentionally tolerant: an unrecognised line is a
no-op rather than an error. New mineru versions can add or rename
log lines without breaking ingestion; the worst case is the UI
loses a particular progress signal until the regex is updated."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from j1.runs import (
    PROGRESS_EVENT_STEP_COMPLETED,
    PROGRESS_EVENT_STEP_PROGRESS,
)

__all__ = [
    "MinerUProgressEvent",
    "MinerUProgressParser",
    "parse_mineru_line",
]

_ENGINE_NAME = "MinerU"


@dataclass(frozen=True)
class MinerUProgressEvent:
    """Normalised progress signal extracted from one mineru log line.

 `stage` matches the canonical execution-plan stage labels
 (`COMPILE`). `step` is the substep within that stage (e.g.
 `LAYOUT_PREPARATION`, `MODEL_FETCH`). `engine` is always
 `MinerU` so downstream consumers can group / filter by parser.
 `progress_percent` is 0..100; `current` / `total` come from the
 underlying tqdm-style line when present."""

    event_type: str          # always "step.progress" or "step.completed"
    stage: str
    step: str
    engine: str
    progress_percent: int | None = None
    current: int | None = None
    total: int | None = None
    message: str | None = None


# ---- Patterns ---------------------------------------------------
#
# Each pattern is a (compiled regex, builder function) pair. The
# builder receives the regex match and returns a `MinerUProgressEvent`
# (or None if the match should be ignored).
#
# Patterns are applied in order; first match wins. Designed for the
# log format observed during real runs (see `parse_mineru_line` tests
# for the canonical input examples).

# `Layout Preparation: 80% | 35/44`
_LAYOUT_PROGRESS_RE = re.compile(
    r"Layout Preparation:\s*(?P<pct>\d+)%\s*\|\s*(?P<cur>\d+)/(?P<tot>\d+)",
    re.IGNORECASE,
)

# `Layout Preparation: 100%|██████████| 1/1 [00:00<00:00, 43.03it/s]` —
# tqdm format that mineru emits via the bundled `loguru` adapter.
_LAYOUT_TQDM_RE = re.compile(
    r"Layout Preparation:\s*(?P<pct>\d+)%\|[^|]*\|\s*(?P<cur>\d+)/(?P<tot>\d+)",
    re.IGNORECASE,
)

# `Fetching 13 files: 8%|▊ | 1/13 [00:00<00:03, 3.03it/s]`
_FETCH_RE = re.compile(
    r"Fetching\s+(?P<total_decl>\d+)\s+files:\s*(?P<pct>\d+)%[^|]*\|[^|]*\|\s*(?P<cur>\d+)/(?P<tot>\d+)",
    re.IGNORECASE,
)

# `get transformers predictor cost: 50.12s` — completion marker for
# the model-load substep. Surface as a 100% progress hit.
_PREDICTOR_LOADED_RE = re.compile(
    r"get\s+transformers\s+predictor\s+cost:\s*(?P<seconds>[\d.]+)s",
    re.IGNORECASE,
)


def _build_layout_event(m: re.Match) -> MinerUProgressEvent:
    cur = int(m.group("cur"))
    tot = int(m.group("tot"))
    pct = int(m.group("pct"))
    return MinerUProgressEvent(
        event_type=PROGRESS_EVENT_STEP_PROGRESS,
        stage="COMPILE",
        step="LAYOUT_PREPARATION",
        engine=_ENGINE_NAME,
        progress_percent=pct,
        current=cur,
        total=tot,
        message=f"Layout preparation: {cur}/{tot} pages",
    )


def _build_fetch_event(m: re.Match) -> MinerUProgressEvent:
    cur = int(m.group("cur"))
    tot = int(m.group("tot"))
    pct = int(m.group("pct"))
    return MinerUProgressEvent(
        event_type=PROGRESS_EVENT_STEP_PROGRESS,
        stage="COMPILE",
        step="MODEL_FETCH",
        engine=_ENGINE_NAME,
        progress_percent=pct,
        current=cur,
        total=tot,
        message=f"Fetching model files: {cur}/{tot}",
    )


def _build_predictor_loaded(m: re.Match) -> MinerUProgressEvent:
    seconds = m.group("seconds")
    return MinerUProgressEvent(
        event_type=PROGRESS_EVENT_STEP_COMPLETED,
        stage="COMPILE",
        step="MODEL_LOAD",
        engine=_ENGINE_NAME,
        progress_percent=100,
        message=f"Model loaded in {seconds}s",
    )


# Order matters — try the more specific tqdm pattern before the
# simple plain-text layout one, otherwise the simpler regex matches
# the tqdm prefix and we lose the count.
_PATTERNS = (
    (_LAYOUT_TQDM_RE, _build_layout_event),
    (_LAYOUT_PROGRESS_RE, _build_layout_event),
    (_FETCH_RE, _build_fetch_event),
    (_PREDICTOR_LOADED_RE, _build_predictor_loaded),
)


def parse_mineru_line(line: str) -> MinerUProgressEvent | None:
    """Try each pattern in order; return the first match's event.

 Returns `None` for unrecognised lines — the caller should swallow
 those rather than treat them as parse errors."""
    if not line:
        return None
    for pattern, builder in _PATTERNS:
        match = pattern.search(line)
        if match is not None:
            return builder(match)
    return None


class MinerUProgressParser:
    """Stateful parser that batches lines and de-duplicates events.

 Use when you have a stream of log lines (from a logging handler
 or stdout reader) and want to emit only the meaningful progress
 deltas. Stateless callers can just call the module-level
 `parse_mineru_line` directly.

 Throttling is the reporter's job (see
 `AuditProgressReporter.report_step_progress`'s 5% threshold) — this
 parser only de-duplicates exact same-percent progress events so
 we don't repeat the SAME line twice (mineru sometimes does)."""

    def __init__(self) -> None:
        self._last_percent: dict[tuple[str, str], int] = {}

    def feed(self, lines: Iterable[str]) -> Iterable[MinerUProgressEvent]:
        for line in lines:
            event = parse_mineru_line(line)
            if event is None:
                continue
            key = (event.stage, event.step)
            previous = self._last_percent.get(key)
            if (
                event.progress_percent is not None
                and previous == event.progress_percent
                and event.event_type == "step.progress"
            ):
                # Duplicate tick — drop.
                continue
            if event.progress_percent is not None:
                self._last_percent[key] = event.progress_percent
            yield event

    def feed_one(self, line: str) -> MinerUProgressEvent | None:
        for event in self.feed([line]):
            return event
        return None
