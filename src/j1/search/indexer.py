import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from j1.artifacts.models import ArtifactRecord
from j1.artifacts.registry import ArtifactRegistry
from j1.errors.exceptions import SearchIndexerError
from j1.intake.registry import SourceRegistry
from j1.processing.results import ProcessingResult
from j1.processing.status import ResultStatus
from j1.projects.context import ProjectContext
from j1.query.scope import QueryScope, RunScope, WorkspaceScope, default_scope
from j1.workspace.resolver import WorkspaceResolver

DEFAULT_DB_FILENAME = "index.db"
MAX_INDEXED_BYTES = 1 * 1024 * 1024  # 1 MiB per artifact

_TABLE_NAME = "artifacts"

# Schema note: `run_id` and `chunk_id` are server-derived columns —
# they're populated from the artifact's metadata at index time so
# downstream consumers (validation, lineage) get trusted IDs they can
# match against without re-reading the registry. Both are UNINDEXED:
# we filter on equality, never full-text search them.
_CREATE_TABLE_SQL = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {_TABLE_NAME} USING fts5(
 artifact_id UNINDEXED,
 artifact_type UNINDEXED,
 title,
 extracted_text,
 source_document_id UNINDEXED,
 source_location UNINDEXED,
 confidence UNINDEXED,
 review_status UNINDEXED,
 checksum UNINDEXED,
 created_at UNINDEXED,
 byte_size UNINDEXED,
 run_id UNINDEXED,
 chunk_id UNINDEXED
)
"""

_INSERT_SQL = f"""
INSERT INTO {_TABLE_NAME} (
 artifact_id, artifact_type, title, extracted_text,
 source_document_id, source_location, confidence,
 review_status, checksum, created_at, byte_size,
 run_id, chunk_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_DELETE_SQL = f"DELETE FROM {_TABLE_NAME} WHERE artifact_id = ?"

_SELECT_COLUMNS = (
    "artifact_id, artifact_type, title, extracted_text, "
    "source_document_id, source_location, confidence, "
    "review_status, checksum, created_at, byte_size, "
    "run_id, chunk_id"
)


@dataclass(frozen=True)
class SearchHit:
    artifact_id: str
    artifact_type: str
    title: str
    source_document_id: str | None
    source_location: str | None
    confidence: float
    review_status: str
    checksum: str
    created_at: str
    byte_size: int
    extracted_text: str
    score: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)
    # Server-derived from the indexed artifact's metadata. `run_id` is
    # populated for every artifact registered during a run; `chunk_id`
    # only for artifacts of kind `chunk` (the canonical chunk artifact
    # carries it in metadata via the RAGAnything bridge). Both are
    # `None` on the DTO when the underlying column is empty so the FE
    # can branch cleanly.
    run_id: str | None = None
    chunk_id: str | None = None


class SqliteSearchIndexer:
    kind: str = "sqlite_search_indexer"

    def __init__(
        self,
        workspace: WorkspaceResolver,
        artifacts: ArtifactRegistry,
        sources: SourceRegistry | None = None,
        *,
        db_filename: str = DEFAULT_DB_FILENAME,
        kind: str | None = None,
    ) -> None:
        if kind:
            self.kind = kind
        self._workspace = workspace
        self._artifacts = artifacts
        self._sources = sources
        self._db_filename = db_filename
        _ensure_fts5_available()

    # ---- SearchIndexer protocol ----------------------------------------

    def index(
        self, ctx: ProjectContext, artifact_ids: list[str]
    ) -> ProcessingResult:
        try:
            indexed = self._index_records(ctx, artifact_ids)
        except Exception as exc:
            return ProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )
        return ProcessingResult(
            status=ResultStatus.SUCCEEDED,
            metadata={"indexed_count": str(indexed)},
        )

    # ---- Convenience ---------------------------------------------------

    def build_full_index(self, ctx: ProjectContext) -> ProcessingResult:
        try:
            records = self._artifacts.list_artifacts(ctx)
        except Exception as exc:
            return ProcessingResult(
                status=ResultStatus.FAILED,
                error=str(exc),
                message=type(exc).__name__,
            )
        return self.index(ctx, [r.artifact_id for r in records])

    # ---- Retrieval -----------------------------------------------------

    def search(
        self,
        ctx: ProjectContext,
        query: str,
        *,
        artifact_types: list[str] | None = None,
        max_results: int = 20,
        scope: QueryScope | None = None,
    ) -> list[SearchHit]:
        if not query.strip():
            return []
        db_path = self._db_path(ctx)
        if not db_path.exists():
            return []
        sanitized = _sanitize_fts_query(query)
        if not sanitized:
            return []
        scope = scope if scope is not None else default_scope()

        sql = (
            f"SELECT {_SELECT_COLUMNS}, bm25({_TABLE_NAME}) AS score "
            f"FROM {_TABLE_NAME} "
            f"WHERE {_TABLE_NAME} MATCH ?"
        )
        params: list = [sanitized]
        if artifact_types:
            placeholders = ",".join("?" for _ in artifact_types)
            sql += f" AND artifact_type IN ({placeholders})"
            params.extend(artifact_types)
        # Scope filter sits in the WHERE clause, BEFORE ORDER BY score
        # / LIMIT, so BM25 ranks only rows that survived the run-id
        # filter. Post-topK pruning would distort the ranking.
        scope_sql, scope_params = _scope_to_sql(scope)
        if scope_sql:
            sql += f" {scope_sql}"
            params.extend(scope_params)
        sql += " ORDER BY score LIMIT ?"
        params.append(max_results)

        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(sql, params)
            return [_row_to_hit(row, with_score=True) for row in cursor.fetchall()]

    def retrieve_by_id(
        self, ctx: ProjectContext, artifact_id: str
    ) -> SearchHit | None:
        db_path = self._db_path(ctx)
        if not db_path.exists():
            return None
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME} WHERE artifact_id = ?",
                (artifact_id,),
            )
            row = cursor.fetchone()
            return _row_to_hit(row, with_score=False) if row else None

    def list_indexed(
        self,
        ctx: ProjectContext,
        *,
        artifact_types: list[str] | None = None,
    ) -> list[SearchHit]:
        db_path = self._db_path(ctx)
        if not db_path.exists():
            return []
        sql = f"SELECT {_SELECT_COLUMNS} FROM {_TABLE_NAME}"
        params: list = []
        if artifact_types:
            placeholders = ",".join("?" for _ in artifact_types)
            sql += f" WHERE artifact_type IN ({placeholders})"
            params.extend(artifact_types)
        sql += " ORDER BY created_at"
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(sql, params)
            return [_row_to_hit(row, with_score=False) for row in cursor.fetchall()]

    # ---- Internals -----------------------------------------------------

    def _index_records(
        self, ctx: ProjectContext, artifact_ids: list[str]
    ) -> int:
        db_path = self._db_path(ctx)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        indexed = 0
        with sqlite3.connect(db_path) as conn:
            conn.execute(_CREATE_TABLE_SQL)
            for aid in artifact_ids:
                record = self._artifacts.get(ctx, aid)
                title = self._derive_title(record)
                text = self._extract_text(ctx, record)
                source_document_id = (
                    record.source_document_ids[0]
                    if record.source_document_ids
                    else ""
                )
                source_location = str(record.metadata.get("source_location", ""))
                confidence = float(record.metadata.get("confidence", 0.0))
                # Server-derived: read straight from the artifact record's
                # metadata so downstream consumers can trust these fields
                # without a second lookup. Empty string when the producer
                # didn't tag (e.g. legacy/non-chunk artifacts) — the row-
                # to-hit mapper turns "" back into None for the DTO.
                run_id = str(record.metadata.get("run_id", ""))
                chunk_id = str(record.metadata.get("chunk_id", ""))
                conn.execute(_DELETE_SQL, (record.artifact_id,))
                conn.execute(
                    _INSERT_SQL,
                    (
                        record.artifact_id,
                        record.kind,
                        title,
                        text,
                        source_document_id,
                        source_location,
                        confidence,
                        record.review_status.value,
                        record.content_hash,
                        record.created_at.isoformat(),
                        record.byte_size,
                        run_id,
                        chunk_id,
                    ),
                )
                indexed += 1
            conn.commit()
        return indexed

    def _db_path(self, ctx: ProjectContext) -> Path:
        return self._workspace.search(ctx) / self._db_filename

    def _derive_title(self, record: ArtifactRecord) -> str:
        explicit = record.metadata.get("title")
        if explicit:
            return str(explicit)
        return f"{record.kind}/{record.artifact_id}"

    def _extract_text(
        self, ctx: ProjectContext, record: ArtifactRecord
    ) -> str:
        path = self._workspace.project_root(ctx) / record.location
        if not path.is_file():
            return ""
        raw = path.read_bytes()[:MAX_INDEXED_BYTES]
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return ""


_FTS_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _sanitize_fts_query(query: str) -> str:
    """Strip punctuation and join tokens with OR for natural-language queries.

 Why: FTS5 treats `?`, `:`, `*`, `(`, `)` as query operators, so a question
 like "where is the requirement?" raises a syntax error. Implicit AND
 (FTS5's default for space-separated tokens) is also too strict — common
 stop words drop recall to zero on natural-language input. Joining with OR
 matches any token; BM25 ranking still surfaces the best matches first.
 """
    tokens = _FTS_TOKEN_RE.findall(query)
    return " OR ".join(tokens)


def _ensure_fts5_available() -> None:
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.execute("CREATE VIRTUAL TABLE _fts_check USING fts5(a)")
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        raise SearchIndexerError(
            f"SQLite FTS5 module is not available in this Python build: {exc}"
        ) from exc


def _row_to_hit(row, *, with_score: bool) -> SearchHit:
    (
        artifact_id,
        artifact_type,
        title,
        extracted_text,
        source_document_id,
        source_location,
        confidence,
        review_status,
        checksum,
        created_at,
        byte_size,
        run_id,
        chunk_id,
        *score_tail,
    ) = row
    return SearchHit(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        title=title,
        source_document_id=source_document_id or None,
        source_location=source_location or None,
        confidence=float(confidence) if confidence is not None else 0.0,
        review_status=review_status,
        checksum=checksum,
        created_at=created_at,
        byte_size=int(byte_size) if byte_size is not None else 0,
        extracted_text=extracted_text,
        score=float(score_tail[0]) if with_score and score_tail else 0.0,
        run_id=run_id or None,
        chunk_id=chunk_id or None,
    )


def _scope_to_sql(scope: QueryScope) -> tuple[str, list]:
    """Translate a `QueryScope` into a SQL fragment + bind parameters.

 Returned fragment is ANDed onto the caller's existing WHERE clause.
 `WorkspaceScope` is the no-op default (empty fragment). `RunScope`
 appends an exact-match equality on `run_id` so BM25 ranking sees
 only rows from that run.
 """
    if isinstance(scope, RunScope):
        return ("AND run_id = ?", [scope.run_id])
    if isinstance(scope, WorkspaceScope):
        return ("", [])
    # Defensive: an unknown scope subtype gets the workspace-default
    # behaviour so we never accidentally widen results when a future
    # scope subclass is introduced before its filter clause exists.
    return ("", [])
