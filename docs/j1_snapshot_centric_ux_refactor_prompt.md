# Prompt: Refactor J1 UX Around Document, Snapshot, Run, Query Scope, and Validation

## Context

The backend is being corrected to use a snapshot-centric model:

- `Document` = source file / knowledge source.
- `DocumentSnapshot` = queryable/publishable knowledge version.
- `IngestionRun` = processing/execution attempt that creates a snapshot.
- `ArtifactRecord` = output owned by a snapshot, with `created_by_run_id` for lineage.
- `DocumentRecord.knowledge_state` = attached / detached / removed visibility.
- `DocumentRecord.active_snapshot_id` = current active knowledge version for that document.
- Query must always resolve to `snapshot_id`, not `run_id`.

Important mental model:

```text
Snapshot is the knowledge version.
Run is only the processing/execution log that produced a snapshot.
```

The current UI still makes users think that `Run` is the active knowledge version. This causes confusion around re-index, validation, manual query, delete run, refresh enrichment, and active knowledge status.

Your task is to refactor the UX so the UI follows the snapshot-centric model clearly.

Do not start this UX refactor until the backend query-scope fix is completed:

- RAGAnything query uses `snapshot_id`.
- BM25 filters by eligible `snapshot_id`.
- Global fallback risk is removed or blocked.
- Query supports explicit scope for project/document/snapshot.

---

# UX Goals

## Primary UX Goal

Make the user understand this clearly:

```text
Document = what I uploaded
Snapshot = current/candidate knowledge version
Run = technical processing history
```

The main user-facing experience should be around:

```text
Document
→ Active Knowledge Snapshot
→ Candidate Snapshot, if any
→ Query / Validate / Promote / Reject
```

Run should be presented as technical execution detail only.

---

# Required UX Model

There are three query entry points. They use the same backend query capability but have different scopes and user goals.

## 1. Home / Global Knowledge Query

Purpose:

```text
Ask questions across the whole active project knowledge base.
```

Default scope:

```text
All attached documents
→ their active_snapshot_id
```

UI name:

```text
Ask Knowledge Base
```

or:

```text
Global Knowledge Query
```

Should not expose run details by default.

Expected behavior:

```text
Query scope = project_active
Includes only attached documents with active snapshots.
Excludes detached, removed, failed, candidate, superseded snapshots.
```

---

## 2. Document Detail Manual Query

Purpose:

```text
Test the current active knowledge state of this document.
```

Default scope:

```text
this document's active_snapshot_id
```

UI name:

```text
Test Active Knowledge
```

or:

```text
Manual Query Trace
```

This belongs in `Document Detail`, not `Run Detail`.

It should help answer:

```text
Is this document's active snapshot queryable?
Is retrieval using the correct snapshot?
Is the document leaking old/candidate snapshot data?
Does RAGAnything and BM25 return expected evidence?
```

Expected behavior:

```text
Query scope = document_active
document_id = current document
snapshot_ids = [document.active_snapshot_id]
```

If the document is detached, the UI should clearly show:

```text
This document is detached from project knowledge.
Manual testing is available for inspection, but it is not included in global query.
```

---

## 3. Run Validation Query

Purpose:

```text
Evaluate whether the candidate snapshot produced by this run is good enough to publish.
```

Default scope:

```text
snapshot produced by this run
```

Usually:

```text
run.target_snapshot_id
```

or:

```text
snapshot.created_by_run_id = run.id
```

UI name:

```text
Validate Produced Snapshot
```

or:

```text
Test Candidate Knowledge
```

This belongs in `Run Detail > Validation`.

Do not describe it as “query this run”. The run is not queryable. The run only points to the produced snapshot.

Expected behavior:

```text
Query scope = snapshot_candidate
snapshot_ids = [run.target_snapshot_id]
```

This should help the user decide:

```text
Approve & Publish candidate snapshot
Reject candidate snapshot
Keep current active snapshot unchanged
```

---

# Document Detail UX

Refactor Document Detail so it is snapshot-centered.

Recommended sections/tabs:

```text
Overview
Active Knowledge
Candidate Knowledge
Test Active Knowledge
Validation Summary
Processing History
Artifacts / Reports
```

## Overview

Show:

```text
Document name
Knowledge state: attached / detached / removed
Lifecycle status
Active snapshot ID / version label
Active snapshot status
Last promoted time
Created by run ID
Current operation: none / re-indexing / validating / removing / attaching / detaching
```

Avoid presenting `active run` as the primary state.

Use:

```text
Active Snapshot
Created by Processing Run
```

Do not use:

```text
Active Run
```

---

## Active Knowledge Section

Show the current active snapshot:

```text
Active Knowledge Snapshot
- Snapshot ID
- Version label
- Status: Active / Ready
- Created by Run
- Promoted at
- Validation status, if available
- Artifact summary
- Query availability
```

Actions:

```text
Test Active Knowledge
Refresh Enrichment for Active Snapshot
Re-index Document
Detach from Knowledge
Remove Knowledge
```

Do not put `Delete Run` here.

---

## Candidate Knowledge Section

Show this only when there is a candidate snapshot from an in-progress or recently completed run.

Candidate snapshot states may include:

```text
BUILDING
READY
PENDING_REVIEW
VALIDATING
VALIDATION_FAILED
REJECTED
PROMOTED
```

Show:

```text
Candidate Knowledge Version
- Candidate snapshot ID
- Created by processing run
- Run status
- Build status
- Validation status
- Indexed at / completed at
- Whether it is queryable globally: No, pending promotion
```

Actions:

```text
Open Validation
Test Candidate Knowledge
View Processing Run
Approve & Publish
Reject Candidate
Delete Candidate / Delete Non-active Run
```

Important copy:

```text
This candidate is not used by global knowledge query until it is approved and promoted.
```

If validation-before-promotion is enabled:

```text
Approve & Publish should be disabled until required validation passes.
```

If validation-before-promotion is disabled:

```text
Still show review information, but do not block promotion unless backend requires it.
```

---

## Test Active Knowledge Tab

This is the Document Detail manual query trace.

Default scope:

```text
document_active
```

The UI should show a scope badge:

```text
Scope: This document's active snapshot
Snapshot: <active_snapshot_id>
```

Query result should show:

```text
Answer
RAGAnything result
BM25 evidence
Citations/source chunks
Snapshot IDs used
Provider diagnostics
Whether BM25 participated in answer
Whether RAGAnything was used
Errors/warnings
```

Add an explicit warning if the backend returns data from any snapshot other than the document active snapshot.

---

## Processing History

This replaces a run-centered main view.

Show a table/list:

```text
Run ID
Run type: initial_index / reindex / refresh_enrich
Produced snapshot ID
Run status
Snapshot state
Started at
Completed at
Actions
```

Actions per run:

```text
View Processing Run
Open Validation
Delete Processing Run, only if allowed
```

Do not allow:

```text
Set run active
Resume run as knowledge
Query run directly
```

---

# Run Detail UX

Run Detail should become a technical/operator page.

Recommended tabs:

```text
Summary
Ingestion Trace
Stage Timeline
Produced Artifacts
Validation
Logs / Diagnostics
```

## Run Summary

Show:

```text
Processing Run
- Run ID
- Run type
- Status
- Started at
- Completed at
- Parent run, if any
- Target / produced snapshot ID
- Associated document
```

Clearly state:

```text
This run is an execution record. The produced snapshot is the knowledge version.
```

---

## Validation Tab

This is the main user-facing purpose of Run Detail.

Default scope:

```text
run.target_snapshot_id
```

Show:

```text
Validate Produced Snapshot
- Candidate snapshot ID
- Current active snapshot ID for the document
- Validation status
- Test cases
- Manual query input
- Query trace for candidate snapshot
- Pass/fail summary
- Approve & Publish
- Reject Candidate
```

The validation query must use snapshot scope:

```text
scope_type = snapshot_candidate
snapshot_ids = [run.target_snapshot_id]
```

Do not use `run_id` as query scope.

---

## Ingestion Trace

This is not the same as manual query trace.

Show technical processing trace:

```text
Assessment
Compile
Enrichment
Graph/index
Validation
Promotion
Cleanup
Retry count
Timing
Warnings/errors
```

This trace is for understanding why indexing was slow or failed.

---

# Blue / Green UX

Implement blue/green wording where useful.

```text
Blue = current active snapshot
Green = candidate snapshot from a new run
```

In UI copy, use user-friendly labels:

```text
Current Active Knowledge
New Candidate Knowledge
```

Technical tooltip can mention:

```text
Blue/green publish model: the new version is built and reviewed separately before replacing the current active version.
```

Expected flow:

```text
Current active snapshot remains queryable.
New re-index creates candidate snapshot.
Candidate snapshot is tested in validation scope.
If approved, candidate becomes active.
Old active snapshot becomes superseded.
If rejected, current active snapshot stays active.
```

---

# Action Rules

## Re-index Document

Should be launched from Document Detail.

Expected copy:

```text
Create a new candidate knowledge version from this document.
The current active knowledge will remain available until the new version is approved.
```

Do not say:

```text
Replace active run
```

---

## Approve & Publish

Should promote candidate snapshot.

Before enabling, check:

```text
candidate snapshot exists
candidate is READY / reviewable
backend allows promotion
required validation passed, if configured
document is not removed
```

Expected result:

```text
candidate snapshot becomes active
old active snapshot becomes superseded
global query now uses candidate snapshot
```

---

## Reject Candidate

Should mark candidate as rejected / validation_failed / not promoted.

Expected result:

```text
current active snapshot remains unchanged
candidate is not included in global query
```

---

## Delete Processing Run

Should be available only for non-active/non-protected runs.

UI should block deletion if:

```text
run produced the current active snapshot
run is running
run is the only usable processing history where backend disallows deletion
```

Use backend guard, but also show a clear UI message:

```text
This run cannot be deleted because it produced the active knowledge snapshot.
```

---

## Refresh Enrichment

This action should belong to active snapshot/document context, not generic run context.

Use wording:

```text
Refresh Enrichment for Active Snapshot
```

It may create a new candidate snapshot if backend behavior does that. If so, UI must show it as candidate knowledge pending review/promotion.

---

## Remove Knowledge

This is stronger than detach.

Expected copy:

```text
Remove this document and its knowledge from the project. This will delete related processing outputs and it cannot be used in query.
```

Do not confuse it with `Delete Run`.

---

## Detach Knowledge

Expected copy:

```text
Detach this document from project knowledge. Its data is preserved but excluded from global query.
```

---

# Query Scope API Contract

The frontend should call backend query APIs using explicit scope.

Proposed frontend query modes:

```text
project_active
document_active
snapshot_explicit
snapshot_candidate
```

Expected mapping:

```text
Home Query:
  scope_type = project_active
  project_id = current project

Document Detail Query:
  scope_type = document_active
  document_id = current document

Run Validation Query:
  scope_type = snapshot_candidate
  run_id = current run
  snapshot_id = run.target_snapshot_id if already available

Explicit Debug Query:
  scope_type = snapshot_explicit
  snapshot_ids = [...]
```

The UI should display the resolved scope returned by backend:

```text
Resolved snapshot IDs
Documents included
Documents excluded
Providers used
Warnings
```

---

# UI Wording Changes

Replace confusing terms:

```text
Active Run
→ Active Knowledge Snapshot

Run Version
→ Processing Run

Query Run
→ Test Produced Snapshot

Run Validation
→ Validate Produced Snapshot

Re-process Run
→ Re-index Document / Create New Candidate

Refresh Run
→ Refresh Enrichment for Active Snapshot

Set Active Run
→ Approve & Publish Candidate Snapshot

Delete Active Run
→ Not allowed
```

---

# Required Screens / Components To Review

Please inspect the frontend and identify exact files/components for:

```text
Document Detail page
Run Detail page
Validation tab
Manual query / query trace components
Knowledge action buttons
Run history table
Document action menu
Home / global query page
Status badges
Primary status panel
```

Then refactor them according to the model above.

Do not rename backend fields unless already supported. UI can map existing fields to clearer labels.

---

# Backend Dependency Assumptions

Assume backend provides or will provide:

```text
document.active_snapshot_id
document.knowledge_state
snapshot status/state
run.target_snapshot_id
snapshot.created_by_run_id
query endpoint with explicit scope
promotion endpoint for candidate snapshot
reject candidate endpoint, or equivalent status update
delete run endpoint with active snapshot protection
```

If an endpoint is missing, do not fake behavior in the UI. Add a TODO in the report and wire the UI only where backend support exists.

---

# Acceptance Criteria

## UX Clarity

- User can clearly see current active knowledge snapshot for a document.
- User can clearly see candidate snapshot after re-index.
- User understands candidate is not in global query until promoted.
- Run is presented as technical processing history, not knowledge version.
- Manual query trace is available on Document Detail for active snapshot.
- Validation query is available on Run Detail for produced candidate snapshot.
- Global query is available from Home / project level and uses all attached active snapshots.

## Scope Correctness

- Home query uses project active snapshots only.
- Document Detail query uses that document's active snapshot only.
- Run Validation query uses the produced snapshot only.
- UI displays resolved snapshot IDs from backend.
- UI warns if returned scope does not match expected scope.

## Action Correctness

- Re-index creates candidate knowledge, does not imply immediate replacement.
- Approve & Publish promotes candidate snapshot.
- Reject keeps current active snapshot.
- Delete Run is blocked for active-snapshot-producing run.
- Detach excludes document from global query but preserves data.
- Remove Knowledge deletes/excludes document and related knowledge as backend supports.

## Documentation

Update relevant docs after implementation:

```text
README or docs overview
Document / Snapshot / Run model doc
Ingestion flow doc
Query flow doc
Validation flow doc
UI usage guide / operator guide
```

Docs must clearly explain:

```text
Document = source file
Snapshot = knowledge version
Run = processing execution log
Global query = all attached active snapshots
Document query = active snapshot of one document
Run validation query = candidate snapshot produced by run
Blue/green publish = current active snapshot remains live until candidate is approved
```

## Tests

Add or update tests for:

```text
Document Detail shows active snapshot, not active run
Candidate snapshot appears after re-index
Document manual query sends document_active scope
Run validation query sends snapshot_candidate scope
Home query sends project_active scope
Delete active-producing run is disabled or shows backend error
Detached document is excluded from Home query
Candidate snapshot is not included in Home query before promotion
Approve & Publish updates active snapshot UI
Reject candidate keeps old active snapshot UI
```

---

# Output Required From You

Please produce:

1. Implementation summary.
2. Files/components changed.
3. Any backend endpoint gaps discovered.
4. Screenshots or UI state descriptions if screenshots are not possible.
5. Test results.
6. Remaining risks.

Important: Do not preserve legacy UI wording if it contradicts the snapshot-centric model. Remove or rename confusing actions boldly.

---

# Key Principle

Same query engine, three UX scopes:

```text
Home = project active snapshots
Document Detail = this document active snapshot
Run Validation = candidate snapshot produced by run
```

And:

```text
Run is not knowledge. Snapshot is knowledge.
```
