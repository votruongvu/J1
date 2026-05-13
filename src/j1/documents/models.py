from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from j1.jobs.status import ProcessingStatus
from j1.projects.context import ProjectContext


# ---- Knowledge state literal ----
#
# Drives the document-centric lifecycle introduced in the
# document-as-source-of-truth refactor:
#
#  * ``attached``  — document is an active part of the knowledge base.
#    Retrieval, search, validation, domain context aggregation, and
#    answer generation may use it.
#  * ``detached``  — document and its runs/artifacts are preserved,
#    but the knowledge layer must NOT use it. Re-attachable later.
#  * ``removed``   — generated knowledge is no longer usable. The
#    document is hidden from normal UI; resume/re-index from old
#    runs is disabled. A minimal tombstone may still exist for
#    audit but it does not behave like usable knowledge.
#
# Backward compat: every existing `DocumentRecord` written before
# this refactor has no `knowledge_state` field on disk. The store's
# deserializer defaults missing values to ``attached`` so existing
# documents retain their prior "everything is usable" behaviour.
KnowledgeState = Literal["attached", "detached", "removed"]


# ---- Lifecycle status literal ----
#
# Tracks the *operational* state of a document — orthogonal to
# ``KnowledgeState`` (which is the operator-facing visibility gate).
# Set by Remove flow + cleanup service:
#
#  * ``stable``         — default. Document is in a steady state.
#  * ``removing``       — gate-first phase of Remove: query_enabled
#                         already false but cleanup still running.
#  * ``removed``        — cleanup completed successfully.
#  * ``cleanup_failed`` — partial cleanup; operator action required.
#  * ``failed``         — initial ingestion failed terminally;
#                         document is unusable.
#
# The eligibility resolver disqualifies any of the non-``stable``
# states (see ``j1.query.eligibility._DISALLOWED_LIFECYCLE``) so a
# document under cleanup CAN'T leak into queries even if a stale
# ``active_run_id`` lingers during the transition.
LifecycleStatus = Literal[
    "stable", "removing", "removed", "cleanup_failed", "failed",
]


# ---- Pending operation literal ----
#
# Per-document mutating-operation lock. Only one of these may be
# active at a time on a document (CAS-acquired by the dispatch
# layer). Used to reject concurrent re-index / detach / remove
# requests with a 409 rather than racing them.
PendingOperation = Literal[
    "reindex", "refresh_enrich", "detach", "attach", "remove",
]


@dataclass(frozen=True)
class SourceDocument:
    uri: str
    content_type: str | None = None
    title: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DocumentRecord:
    """Document-centric source-of-truth record.

    Carries lifecycle state plus pointers to the document's current
    usable run + most recent uploaded version. The pre-refactor
    fields (document_id..created_at) are preserved and unchanged so
    every existing read path keeps working without modification.

    The three new fields are optional with safe defaults:

    * ``knowledge_state`` — see ``KnowledgeState`` literal above.
      Defaults to ``attached`` on read when missing on disk so
      pre-refactor documents remain visible to retrieval.

    * ``active_run_id`` — the run whose result the document layer
      considers "current usable". Selection rule lives in the
      backfill helper (`select_active_run_id` in
      `j1.documents.lifecycle`):
        1. latest succeeded run, else
        2. latest failed run with a compile checkpoint, else
        3. latest run by ``created_at``.
      ``None`` when the document has no terminal runs yet (e.g.
      just uploaded, ingestion still queued).

    * ``latest_version_id`` — pointer to the most recent
      ``DocumentVersion`` for this document. ``None`` for legacy
      records until the backfill creates an initial version.

    * ``removed_at`` — set when ``knowledge_state`` flips to
      ``removed``. Kept separately from ``status`` (which is the
      *ingestion* outcome of the upload itself) so we don't conflate
      "the upload succeeded" with "the knowledge is currently
      usable".
    """

    document_id: str
    project: ProjectContext
    original_filename: str
    stored_filename: str
    mime_type: str | None
    file_size: int
    checksum: str
    status: ProcessingStatus
    created_at: datetime

    # ---- New document-centric fields (all optional + defaulted) ---
    knowledge_state: KnowledgeState = "attached"
    active_run_id: str | None = None
    latest_version_id: str | None = None
    removed_at: datetime | None = None
    updated_at: datetime | None = None

    # ---- Lifecycle + operation-lock fields -----------------------
    # ``lifecycle_status`` is the operational state (see literal
    # above). Defaults to ``stable`` on read when the column is
    # missing on disk so legacy records keep their existing
    # behaviour.
    lifecycle_status: LifecycleStatus = "stable"

    # Per-document mutating-operation lock. CAS-acquired by the
    # dispatcher before re-index / detach / remove / refresh-enrich
    # so concurrent mutations on the same document are rejected
    # with 409. ``None`` = unlocked (no operation in flight).
    #
    # ``pending_operation_run_id`` is the run_id (or operation id)
    # the lock is held for — used for diagnostic logging and to
    # disambiguate "is this the same operation retrying?" from
    # "is a different operation trying to barge in?".
    #
    # ``pending_operation_started_at`` is the wall-clock time the
    # lock was acquired. Used to flag stuck operations during
    # cleanup sweeps (a lock older than the configured TTL is a
    # candidate for forced release after operator inspection).
    pending_operation: PendingOperation | None = None
    pending_operation_run_id: str | None = None
    pending_operation_started_at: datetime | None = None

    @property
    def tenant_id(self) -> str:
        return self.project.tenant_id

    @property
    def project_id(self) -> str:
        return self.project.project_id

    def is_attached(self) -> bool:
        """Convenience: is this document usable for retrieval right now?

        Centralised in one method so callers don't keep
        re-implementing the `state == "attached"` comparison. Any
        future state additions (e.g. ``quarantined``) get plumbed
        through here in one place.
        """
        return self.knowledge_state == "attached"


@dataclass(frozen=True)
class DocumentVersion:
    """One stored version of a document's file content.

    Versions exist so that re-indexing the same uploaded file is
    cheap (reuse the same version_id, reuse compatible immutable
    artifacts) while a re-upload of a *changed* file produces a new
    version under the same document group.

    Identity is content-based: two uploads with the same
    ``file_hash`` under the same ``document_id`` resolve to the same
    version. This is what lets the re-index flow stay idempotent on
    "you uploaded the same bytes again."

    Fields:

    * ``document_version_id`` — opaque uuid; the FK every artifact
      manifest will eventually carry.
    * ``document_id`` — parent document this version belongs to.
    * ``file_hash`` — content-derived hash (matches
      ``DocumentRecord.checksum`` for the initial-version backfill;
      future versions get fresh hashes).
    * ``original_filename`` — preserved across versions because
      Office tools sometimes rename a file when saving.
    * ``storage_uri`` — workspace-relative location of the
      uploaded bytes. Empty for backfilled versions where the
      original upload predates this field (we don't synthesise a
      fake URI).
    * ``mime_type``/``size_bytes`` — operational metadata; useful
      for the FE's "versions" table.
    * ``created_by_run_id`` — the run that *created* this version
      (when the upload happened mid-pipeline). ``None`` for
      backfilled rows because the historical upload path predates
      this field.
    """

    document_version_id: str
    document_id: str
    project: ProjectContext
    file_hash: str
    original_filename: str
    storage_uri: str
    mime_type: str | None
    size_bytes: int
    created_at: datetime
    created_by_run_id: str | None = None

    @property
    def tenant_id(self) -> str:
        return self.project.tenant_id

    @property
    def project_id(self) -> str:
        return self.project.project_id
