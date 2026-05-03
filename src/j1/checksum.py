import hashlib
from pathlib import Path

from j1.artifacts.models import ArtifactRecord
from j1.documents.models import DocumentRecord
from j1.errors.exceptions import ChecksumMismatchError
from j1.projects.context import ProjectContext
from j1.workspace.resolver import WorkspaceResolver

CHECKSUM_PREFIX = "sha256:"
_CHUNK_SIZE = 64 * 1024


def hash_file(path: Path) -> str:
    """Return a `sha256:<hex>` checksum for a file's bytes."""
    hasher = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return f"{CHECKSUM_PREFIX}{hasher.hexdigest()}"


def verify_artifact(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    record: ArtifactRecord,
) -> bool:
    path = workspace.project_root(ctx) / record.location
    if not path.is_file():
        return False
    return hash_file(path) == record.content_hash


def verify_document(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    record: DocumentRecord,
) -> bool:
    path = workspace.raw(ctx) / record.stored_filename
    if not path.is_file():
        return False
    return hash_file(path) == record.checksum


def assert_artifact_integrity(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    record: ArtifactRecord,
) -> None:
    path = workspace.project_root(ctx) / record.location
    if not path.is_file():
        raise ChecksumMismatchError(
            f"artifact content missing on disk: {record.artifact_id}",
            expected=record.content_hash,
            actual=None,
        )
    actual = hash_file(path)
    if actual != record.content_hash:
        raise ChecksumMismatchError(
            f"checksum mismatch for artifact {record.artifact_id}",
            expected=record.content_hash,
            actual=actual,
        )


def assert_document_integrity(
    workspace: WorkspaceResolver,
    ctx: ProjectContext,
    record: DocumentRecord,
) -> None:
    path = workspace.raw(ctx) / record.stored_filename
    if not path.is_file():
        raise ChecksumMismatchError(
            f"document content missing on disk: {record.document_id}",
            expected=record.checksum,
            actual=None,
        )
    actual = hash_file(path)
    if actual != record.checksum:
        raise ChecksumMismatchError(
            f"checksum mismatch for document {record.document_id}",
            expected=record.checksum,
            actual=actual,
        )
