"""ChunkProjector — read `kind="chunk"` artifacts → neutral chunk DTOs.

Accepts a flexible set of input shapes so producers with different
preferences can all feed the review surface:

 * Single-chunk JSON — top-level object, the artifact IS one chunk.
 * Multi-chunk JSON — top-level array OR `{"chunks": [...]}`.
 * NDJSON — one chunk per line (`.ndjson` / `.jsonl`).

Field names on each chunk dict are tolerant of snake_case AND
camelCase (`chunk_id` / `chunkId`, `token_count` / `tokenCount` /
`tokens`, `page_start` / `pageStart`, etc.). When `chunk_id` is
missing, the projector synthesises one from the artifact_id + index
so chunks remain addressable.

The projector never touches LightRAG / vendor formats — those would
land here as a separate input branch if and when the bridge starts
emitting them as `kind="chunk"`. For now only handles the
canonical path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from j1.artifacts.models import ArtifactRecord
from j1.processing.results import ARTIFACT_KIND_CHUNK
from j1.ingestion_review.dtos import (
    ChunkDetailDTO,
    ChunkPreviewDTO,
    LinkedAssetDTO,
)

_log = logging.getLogger("j1.ingestion_review.chunks")

CHUNK_KIND = ARTIFACT_KIND_CHUNK
PREVIEW_MAX_CHARS = 240
_NDJSON_SUFFIXES = frozenset({".ndjson", ".jsonl"})


@dataclass(frozen=True)
class _ChunkRecord:
    """Internal projection — kept separate from the DTOs so we can
 sort / page / filter without paying the pydantic-validation cost
 until we actually return rows."""

    chunk_id: str
    body: str
    page_start: int | None
    page_end: int | None
    section: str | None
    title: str | None
    token_count: int | None
    confidence: float | None
    metadata: dict[str, Any]
    linked_assets: list[LinkedAssetDTO]
    source_artifact_id: str | None
    source_document_ids: list[str]


class ChunkProjector:
    """Projects `kind="chunk"` artifacts into neutral chunk records.

 Constructor takes the path-resolver callable (rather than the
 workspace directly) so the projector can stay agnostic to how
 paths are produced — keeps it unit-testable without a workspace
 fixture."""

    def __init__(
        self,
        *,
        path_resolver,  # callable: ArtifactRecord -> pathlib.Path
    ) -> None:
        self._path_resolver = path_resolver

    # ---- Public surface ---------------------------------------------

    def project_records(
        self, artifacts: list[ArtifactRecord],
    ) -> list[_ChunkRecord]:
        """Read every chunk artifact and return the flat list of
 chunks they contain. Order: artifact-creation order, then
 within-file order, then synthesized fallback."""
        chunks: list[_ChunkRecord] = []
        for artifact in artifacts:
            if artifact.kind != CHUNK_KIND:
                continue
            try:
                path = self._path_resolver(artifact)
            except Exception:  # noqa: BLE001 — projector must not crash
                _log.warning(
                    "chunk artifact %s not readable; skipping",
                    artifact.artifact_id,
                )
                continue
            if not path.is_file():
                _log.warning(
                    "chunk artifact %s missing on disk at %s; skipping",
                    artifact.artifact_id, path,
                )
                continue
            chunks.extend(_parse_artifact(artifact, path))
        return chunks

    @staticmethod
    def to_preview(record: _ChunkRecord) -> ChunkPreviewDTO:
        return ChunkPreviewDTO(
            chunk_id=record.chunk_id,
            preview=_make_preview(record.body),
            page_start=record.page_start,
            page_end=record.page_end,
            section=record.section,
            title=record.title,
            token_count=record.token_count,
            confidence=record.confidence,
            metadata=record.metadata,
            linked_assets=record.linked_assets,
            source_artifact_id=record.source_artifact_id,
        )

    @staticmethod
    def to_detail(
        record: _ChunkRecord, *, lineage: dict[str, Any] | None = None,
    ) -> ChunkDetailDTO:
        return ChunkDetailDTO(
            chunk_id=record.chunk_id,
            body=record.body,
            page_start=record.page_start,
            page_end=record.page_end,
            section=record.section,
            title=record.title,
            token_count=record.token_count,
            confidence=record.confidence,
            metadata=record.metadata,
            linked_assets=record.linked_assets,
            source_artifact_id=record.source_artifact_id,
            lineage=lineage or {},
        )


# ---- Parsing ---------------------------------------------------------


def _parse_artifact(
    artifact: ArtifactRecord, path: Path,
) -> Iterator[_ChunkRecord]:
    """Yield chunk records from one chunk artifact file.

 Format detection:
 * `.ndjson` / `.jsonl` extension → NDJSON.
 * Otherwise JSON. Top-level array, `{"chunks": [...]}`, or
 single object.

 Tolerates malformed entries: a bad line in NDJSON / a bad entry
 in an array is logged and skipped — one bad chunk shouldn't
 blank the whole tab."""
    suffix = PurePosixPath(path.name).suffix.lower()

    if suffix in _NDJSON_SUFFIXES:
        yield from _parse_ndjson(artifact, path)
        return

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning(
            "chunk artifact %s has invalid JSON: %s", artifact.artifact_id, exc,
        )
        return

    if isinstance(raw, list):
        for index, entry in enumerate(raw):
            record = _coerce_chunk(entry, artifact, index)
            if record is not None:
                yield record
        return
    if isinstance(raw, dict):
        chunks = raw.get("chunks")
        if isinstance(chunks, list):
            for index, entry in enumerate(chunks):
                record = _coerce_chunk(entry, artifact, index)
                if record is not None:
                    yield record
            return
        # Single-chunk object — the artifact IS one chunk.
        record = _coerce_chunk(raw, artifact, 0)
        if record is not None:
            yield record
        return

    _log.warning(
        "chunk artifact %s top-level type %s not recognised; skipping",
        artifact.artifact_id, type(raw).__name__,
    )


def _parse_ndjson(
    artifact: ArtifactRecord, path: Path,
) -> Iterator[_ChunkRecord]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        _log.warning(
            "chunk artifact %s unreadable: %s", artifact.artifact_id, exc,
        )
        return
    index = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            _log.warning(
                "skipping malformed NDJSON line in %s", artifact.artifact_id,
            )
            continue
        record = _coerce_chunk(entry, artifact, index)
        if record is not None:
            yield record
            index += 1


def _coerce_chunk(
    entry: Any, artifact: ArtifactRecord, index: int,
) -> _ChunkRecord | None:
    """Project one raw entry into a `_ChunkRecord`.

 Tolerates snake_case + camelCase. Drops entries that don't carry
 a `body`/`content` (a chunk without text isn't useful to review)."""
    if not isinstance(entry, dict):
        return None

    body = _str_field(entry, "body", "content")
    if not body:
        return None

    chunk_id = _str_field(entry, "chunk_id", "chunkId", "id")
    if not chunk_id:
        # Synthesize a stable id derived from the artifact + index.
        # FE can still link straight to the chunk via this id.
        chunk_id = f"{artifact.artifact_id}#{index}"

    linked_assets_raw = entry.get("linked_assets") or entry.get("linkedAssets") or []
    linked_assets: list[LinkedAssetDTO] = []
    if isinstance(linked_assets_raw, list):
        for la in linked_assets_raw:
            if not isinstance(la, dict):
                continue
            artifact_ref = _str_field(la, "artifact_id", "artifactId")
            if not artifact_ref:
                continue
            linked_assets.append(
                LinkedAssetDTO(
                    artifact_id=artifact_ref,
                    kind=_str_field(la, "kind"),
                )
            )

    source_artifact_id = _str_field(
        entry, "source_artifact_id", "sourceArtifactId",
    )
    # If the chunk dict didn't pin a source, fall back to the artifact
    # itself — it IS a source. Matches the lineage the FE expects.
    if not source_artifact_id:
        source_artifact_id = artifact.artifact_id

    return _ChunkRecord(
        chunk_id=chunk_id,
        body=body,
        page_start=_int_field(entry, "page_start", "pageStart"),
        page_end=_int_field(entry, "page_end", "pageEnd"),
        section=_str_field(entry, "section"),
        title=_str_field(entry, "title"),
        token_count=_int_field(entry, "token_count", "tokenCount", "tokens"),
        confidence=_float_field(entry, "confidence"),
        metadata=dict(entry.get("metadata") or {}),
        linked_assets=linked_assets,
        source_artifact_id=source_artifact_id,
        source_document_ids=list(artifact.source_document_ids),
    )


# ---- Field extraction helpers ---------------------------------------


def _str_field(d: dict, *keys: str) -> str | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        text = str(v).strip()
        if text:
            return text
    return None


def _int_field(d: dict, *keys: str) -> int | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return int(v)
        except (TypeError, ValueError):
            continue
    return None


def _float_field(d: dict, *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


# ---- Preview ---------------------------------------------------------


def _make_preview(body: str) -> str:
    """Squash whitespace and truncate to PREVIEW_MAX_CHARS.

 The trailing ellipsis ('…') is one char so the visible cap is
 `PREVIEW_MAX_CHARS` codepoints exactly — keeps row heights
 predictable on the FE."""
    collapsed = " ".join(body.split())
    if len(collapsed) <= PREVIEW_MAX_CHARS:
        return collapsed
    return collapsed[: PREVIEW_MAX_CHARS - 1] + "…"
