"""Domain-enrichment alias extractor + artifact loader.

Producer surface for evidence-backed entity aliases. Domain
Enrichment runs this over the compiled chunk bodies and persists
the result as a ``domain_enrichment_aliases`` artifact under the
candidate snapshot. The query layer's ``AliasResolver`` consumes
the artifact via :func:`load_enrichment_aliases_for_snapshot`.

Design rules:

* **Conservative.** Two pattern families only — ``ALIAS
  (canonical)`` and ``canonical (ALIAS)``. Nothing fuzzier.
* **Evidence-bound.** Every emitted alias carries a snippet of
  source text + the artifact / chunk / page that produced it.
* **No pack pollution.** Static domain-config aliases are a
  separate source the resolver layers on top — this module never
  reads or returns pack-shipped vocabulary.
* **Scoped.** Aliases are stamped with ``run_id`` + ``snapshot_id``
  + ``document_id``. The loader filters by snapshot so cross-snapshot
  leakage is impossible.

The artifact kind ``domain_enrichment_aliases`` carries a JSON
payload with a single ``aliases`` key (list of dicts). Each dict
matches the wire-shape the spec pins:

::

    {
        "canonical": "reinforced concrete",
        "alias": "RC",
        "confidence": 0.86,
        "source": "domain_enrichment",
        "evidence": {
            "run_id": "...",
            "snapshot_id": "...",
            "artifact_id": "...",
            "chunk_id": "...",
            "page": 3,
            "snippet": "RC (reinforced concrete) beams ..."
        }
    }
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, TYPE_CHECKING

from j1.domains.models import (
    ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
    EntityAlias,
)

if TYPE_CHECKING:
    from j1.artifacts.registry import ArtifactRegistry
    from j1.projects.context import ProjectContext


_log = logging.getLogger("j1.processing.enrichment_aliases")


__all__ = [
    "ALIAS_ARTIFACT_KIND",
    "AliasEvidence",
    "ExtractedAlias",
    "extract_aliases_from_text",
    "extract_aliases_from_chunks",
    "build_alias_payload",
    "parse_alias_payload",
    "load_enrichment_aliases_for_snapshot",
    "register_aliases_artifact",
]


ALIAS_ARTIFACT_KIND = "domain_enrichment_aliases"

# Conservative confidence default. Pattern matches with a tight
# alias shape get this baseline; future producers (LLM-refined)
# can pass higher.
_DEFAULT_CONFIDENCE = 0.86

# Snippet width around the matched span. Wide enough for human
# review, tight enough to keep the artifact small.
_SNIPPET_HALF_WIDTH = 80

# Cap on aliases per artifact. Stops a pathological document from
# emitting thousands of false positives.
_MAX_ALIASES_PER_ARTIFACT = 64

# Alias shape: 2-8 characters, ASCII letters + optional digits,
# at least one uppercase letter, no embedded whitespace. Rules out
# common-word noise like ``"the"`` and over-long matches.
_ALIAS_RE = r"[A-Z][A-Za-z0-9]{1,7}"

# Canonical shape: a multi-word lowercase noun phrase. Whitespace
# delimited, 2-6 words, alphanumerics + hyphens. Reject capitalised
# phrases (those are usually proper nouns from sentence starts —
# false positives like ``"In short (IS)"``).
_CANONICAL_RE = r"(?:[a-z][a-z0-9-]*)(?: +[a-z][a-z0-9-]+){1,5}"

# Pattern 1: ``ALIAS (canonical)``
_PATTERN_ALIAS_FIRST = re.compile(
    rf"\b(?P<alias>{_ALIAS_RE})\s*\(\s*(?P<canonical>{_CANONICAL_RE})\s*\)",
)

# Pattern 2: ``canonical (ALIAS)``
_PATTERN_CANONICAL_FIRST = re.compile(
    rf"\b(?P<canonical>{_CANONICAL_RE})\s*\(\s*(?P<alias>{_ALIAS_RE})\s*\)",
)


# Common multi-letter abbreviations that look like aliases but
# would emit a lot of noise — e.g. "PDF" or "HTTP". Producers
# that ship document-supported aliases for these can override.
_STOPLIST_ALIASES: frozenset[str] = frozenset({
    "PDF", "HTTP", "HTTPS", "URL", "URI", "API",
    "JSON", "YAML", "XML", "CSV",
    "USA", "UK", "EU",
})


# Leading determiners / articles to strip when they appear at the
# head of a matched canonical phrase. The regex is greedy enough
# to absorb them; trimming here keeps the stored canonical clean
# for retrieval expansion.
_LEADING_DETERMINERS: frozenset[str] = frozenset({
    "the", "a", "an", "this", "that", "these", "those",
})


@dataclass(frozen=True)
class AliasEvidence:
    """Per-occurrence provenance for an extracted alias.

    Pure data. Stamped onto every ``ExtractedAlias`` so the FE /
    diagnostics surface "this alias came from chunk X of artifact
    Y in run Z" without having to re-read the chunk."""

    run_id: str | None
    snapshot_id: str | None
    artifact_id: str | None
    chunk_id: str | None
    document_id: str | None
    page: int | None
    snippet: str

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "snapshot_id": self.snapshot_id,
            "artifact_id": self.artifact_id,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "page": self.page,
            "snippet": self.snippet,
        }


@dataclass(frozen=True)
class ExtractedAlias:
    """One alias detected by the extractor + its evidence.

    Different from :class:`EntityAlias` (which is the resolver-
    facing bundle): an ``ExtractedAlias`` is a single
    ``(alias, canonical)`` pair with one evidence record. The
    artifact persists a list of these; the loader merges
    same-canonical entries into a single ``EntityAlias`` bundle on
    read."""

    canonical: str
    alias: str
    confidence: float
    evidence: AliasEvidence
    source: str = ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT

    def to_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "alias": self.alias,
            "confidence": self.confidence,
            "source": self.source,
            "evidence": self.evidence.to_dict(),
        }


# ---- Pure extractor ------------------------------------------------


def extract_aliases_from_text(
    text: str,
    *,
    run_id: str | None = None,
    snapshot_id: str | None = None,
    artifact_id: str | None = None,
    chunk_id: str | None = None,
    document_id: str | None = None,
    page: int | None = None,
) -> tuple[ExtractedAlias, ...]:
    """Run the conservative pattern extractor over a chunk's body.

    Returns every match the two patterns surface, deduplicated by
    ``(alias, canonical)`` within the call. The caller supplies the
    evidence context; the extractor stamps every match with it.

    No domain-pack lookup; no LLM call; no I/O. Pure function so
    tests can call it directly without any J1 wiring.
    """
    if not text:
        return ()
    out: list[ExtractedAlias] = []
    seen: set[tuple[str, str]] = set()
    for pattern in (_PATTERN_ALIAS_FIRST, _PATTERN_CANONICAL_FIRST):
        for match in pattern.finditer(text):
            alias = match.group("alias").strip()
            canonical = match.group("canonical").strip().lower()
            if not alias or not canonical:
                continue
            if alias in _STOPLIST_ALIASES:
                continue
            # Strip a leading determiner if the regex's greedy
            # match absorbed one (``the bill of quantities`` →
            # ``bill of quantities``). The canonical we store is
            # what retrieval will look up against, so a clean
            # form here pays off downstream.
            canonical = _strip_leading_determiner(canonical)
            if not canonical:
                continue
            # Reject when the alias's letters don't appear in the
            # canonical: ``"In (something)"`` would match
            # otherwise. We require every uppercase letter in
            # ``alias`` to appear in order somewhere in ``canonical``
            # — the standard "initialism" rule.
            if not _is_initialism_of(alias, canonical):
                continue
            key = (alias, canonical)
            if key in seen:
                continue
            seen.add(key)
            evidence = AliasEvidence(
                run_id=run_id,
                snapshot_id=snapshot_id,
                artifact_id=artifact_id,
                chunk_id=chunk_id,
                document_id=document_id,
                page=page,
                snippet=_snippet_around(text, match.start(), match.end()),
            )
            out.append(ExtractedAlias(
                canonical=canonical, alias=alias,
                confidence=_DEFAULT_CONFIDENCE,
                evidence=evidence,
            ))
            if len(out) >= _MAX_ALIASES_PER_ARTIFACT:
                return tuple(out)
    return tuple(out)


def extract_aliases_from_chunks(
    chunks: Iterable[dict],
    *,
    run_id: str | None,
    snapshot_id: str | None,
    document_id: str | None,
) -> tuple[ExtractedAlias, ...]:
    """Sweep a sequence of chunk dicts and return every alias the
    extractor finds across them.

    Each chunk dict is expected to expose ``body`` (or ``text``)
    plus optional ``artifact_id`` / ``chunk_id`` / ``page``
    metadata. Unknown fields are ignored. The output is
    deduplicated by ``(alias, canonical, evidence.artifact_id,
    evidence.chunk_id)`` so the same alias detected twice in the
    same chunk doesn't double up.
    """
    out: list[ExtractedAlias] = []
    dedup: set[tuple[str, str, str | None, str | None]] = set()
    for chunk in chunks:
        body = chunk.get("body") or chunk.get("text") or ""
        if not body:
            continue
        artifact_id = chunk.get("artifact_id")
        chunk_id = chunk.get("chunk_id")
        page = chunk.get("page") or chunk.get("page_start")
        for record in extract_aliases_from_text(
            body,
            run_id=run_id,
            snapshot_id=snapshot_id,
            artifact_id=artifact_id,
            chunk_id=chunk_id,
            document_id=document_id,
            page=page,
        ):
            key = (record.alias, record.canonical, artifact_id, chunk_id)
            if key in dedup:
                continue
            dedup.add(key)
            out.append(record)
            if len(out) >= _MAX_ALIASES_PER_ARTIFACT:
                return tuple(out)
    return tuple(out)


# ---- Payload helpers ----------------------------------------------


def build_alias_payload(
    aliases: tuple[ExtractedAlias, ...],
) -> dict:
    """Serialise the extracted aliases into the artifact payload
    shape. Stable JSON contract — the loader reads this back."""
    return {
        "schema_version": "1",
        "aliases": [a.to_dict() for a in aliases],
    }


def parse_alias_payload(
    payload: dict,
) -> tuple[EntityAlias, ...]:
    """Read a persisted alias-artifact payload and return the
    resolver-facing ``EntityAlias`` bundles.

    Merge rule: same canonical name → one ``EntityAlias`` entry
    with the union of alias forms. ``confidence`` is the maximum
    across merged entries; ``source`` stays
    ``domain_enrichment``. The first evidence record wins on
    display order; the resolver doesn't consume evidence directly,
    diagnostics do.

    Forgiving — unknown / malformed entries are dropped, not
    raised."""
    raw = payload.get("aliases") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return ()
    by_canonical: dict[str, dict] = {}
    order: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        canonical = (item.get("canonical") or "").strip()
        alias = (item.get("alias") or "").strip()
        if not canonical or not alias:
            continue
        confidence = item.get("confidence")
        try:
            confidence = (
                float(confidence)
                if confidence is not None else _DEFAULT_CONFIDENCE
            )
        except (TypeError, ValueError):
            confidence = _DEFAULT_CONFIDENCE
        existing = by_canonical.get(canonical)
        if existing is None:
            by_canonical[canonical] = {
                "aliases": [alias],
                "confidence": confidence,
            }
            order.append(canonical)
            continue
        if alias not in existing["aliases"]:
            existing["aliases"].append(alias)
        if confidence > existing["confidence"]:
            existing["confidence"] = confidence
    return tuple(
        EntityAlias(
            canonical_name=canonical,
            aliases=tuple(by_canonical[canonical]["aliases"]),
            confidence=by_canonical[canonical]["confidence"],
            source=ENTITY_ALIAS_SOURCE_DOMAIN_ENRICHMENT,
        )
        for canonical in order
    )


# ---- Producer-side artifact registration --------------------------


def register_aliases_artifact(
    *,
    ctx: "ProjectContext",
    artifact_registry: "ArtifactRegistry",
    run_id: str,
    document_id: str,
    snapshot_id: str,
    aliases: tuple[ExtractedAlias, ...],
    actor: str = "system",
) -> str | None:
    """Persist a tuple of extracted aliases as a single
    ``domain_enrichment_aliases`` artifact under the candidate
    snapshot.

    The payload is stamped inline in ``metadata["payload"]`` —
    alias artifacts are small (a few KiB at most) so the registry's
    metadata field is the natural carrier; no workspace path is
    needed. The loader prefers the inline payload but tolerates
    on-disk JSON as a fallback for legacy producers.

    Returns the new artifact id on success, or ``None`` when the
    input list is empty (no artifact is written for zero-alias
    runs). Errors propagate — the caller decides whether to swallow
    them (best-effort during enrichment) or surface them.
    """
    if not aliases:
        return None
    import hashlib
    import json
    import uuid
    from datetime import datetime, timezone
    from j1.artifacts.models import ArtifactRecord
    from j1.jobs.status import ProcessingStatus, ReviewStatus

    payload = build_alias_payload(aliases)
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    content_hash = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    now = datetime.now(timezone.utc)
    artifact_id = f"alias-{uuid.uuid4().hex[:16]}"
    artifact_registry.add(ArtifactRecord(
        artifact_id=artifact_id,
        project=ctx,
        kind=ALIAS_ARTIFACT_KIND,
        location=f"enrichment/aliases/{artifact_id}.json",
        content_hash=content_hash,
        byte_size=len(encoded),
        status=ProcessingStatus.SUCCEEDED,
        review_status=ReviewStatus.NOT_REQUIRED,
        version=1,
        created_at=now,
        updated_at=now,
        source_document_ids=[document_id],
        metadata={
            "snapshot_id": snapshot_id,
            "run_id": run_id,
            "actor": actor,
            "alias_count": len(aliases),
            # Inline payload — the loader prefers this over the
            # ``location`` path so we avoid an extra disk round-trip.
            "payload": payload,
        },
        snapshot_id=snapshot_id,
        created_by_run_id=run_id,
    ))
    return artifact_id


# ---- Loader -------------------------------------------------------


def load_enrichment_aliases_for_snapshot(
    *,
    ctx: "ProjectContext",
    artifact_registry: "ArtifactRegistry",
    document_id: str,
    snapshot_id: str,
    workspace=None,
) -> tuple[EntityAlias, ...]:
    """Read every ``domain_enrichment_aliases`` artifact attached
    to ``(document_id, snapshot_id)`` and return the merged
    ``EntityAlias`` bundles the resolver will consume.

    Returns ``()`` when no alias artifact exists for the scope —
    callers MUST treat that as "no enrichment aliases available"
    and fall back to static / no aliases. Scope is enforced via
    the registry's ``snapshot_id`` filter; cross-snapshot
    leakage is impossible by construction.

    ``workspace`` is consulted only when an artifact carries
    ``location`` instead of an inline payload — most producers
    persist the payload via the registry's metadata field, which
    is loaded inline. The workspace path is the disk-only fallback
    for legacy producers."""
    try:
        records = artifact_registry.list_artifacts(
            ctx, kind=ALIAS_ARTIFACT_KIND,
        )
    except Exception:  # noqa: BLE001 — the loader is read-only
        return ()
    out: list[EntityAlias] = []
    seen_canonicals: set[str] = set()
    for record in records:
        if not _record_matches_snapshot(record, snapshot_id):
            continue
        sources = getattr(record, "source_document_ids", None) or ()
        if document_id not in sources:
            continue
        payload = _load_payload(record, workspace=workspace)
        if not payload:
            continue
        for entry in parse_alias_payload(payload):
            if entry.canonical_name in seen_canonicals:
                continue
            seen_canonicals.add(entry.canonical_name)
            out.append(entry)
    return tuple(out)


# ---- Internals ----------------------------------------------------


def _strip_leading_determiner(canonical: str) -> str:
    """Drop a leading article (``"the"`` / ``"a"`` / ``"an"`` /
    demonstratives) when the greedy regex absorbed one. Only the
    FIRST token is checked — interior determiners are legitimate
    parts of multi-word terms (e.g. ``"sum of the squares"``). Pure
    string trim; never raises."""
    if not canonical:
        return canonical
    tokens = canonical.split()
    if not tokens:
        return canonical
    if tokens[0] in _LEADING_DETERMINERS and len(tokens) >= 3:
        # Keep the canonical at ≥2 words after stripping — the
        # regex already required ≥2 words upstream, so we only
        # strip when ≥3 words remain.
        return " ".join(tokens[1:])
    return canonical


def _is_initialism_of(alias: str, canonical: str) -> bool:
    """Allowlist gate: ``alias``'s uppercase letters must appear
    in order somewhere in ``canonical``. Filters out matches like
    ``In short (IS)`` while accepting genuine initialisms
    (``RC`` → ``reinforced concrete``, ``BOQ`` → ``bill of
    quantities``)."""
    upper_chars = [c for c in alias if c.isupper()]
    if not upper_chars:
        return False
    canonical_lower = canonical.lower()
    needle = "".join(c.lower() for c in upper_chars)
    # Allow gaps — each letter of the alias must appear in the
    # canonical in order. Walk the canonical and tick off each
    # letter.
    idx = 0
    for ch in canonical_lower:
        if idx < len(needle) and ch == needle[idx]:
            idx += 1
    return idx == len(needle)


def _snippet_around(
    text: str, start: int, end: int,
) -> str:
    """Window of context around the match. Whitespace-collapsed
    so multi-line bodies render tightly in diagnostics."""
    lo = max(0, start - _SNIPPET_HALF_WIDTH)
    hi = min(len(text), end + _SNIPPET_HALF_WIDTH)
    snippet = text[lo:hi]
    return " ".join(snippet.split())


def _record_matches_snapshot(record, snapshot_id: str) -> bool:
    """Snapshot-match check that tolerates the dual stamping
    production code uses today (typed field + metadata fallback).
    Mirrors the resolver's ``_artifact_matches_snapshot``."""
    typed = getattr(record, "snapshot_id", None)
    if typed == snapshot_id:
        return True
    meta = getattr(record, "metadata", None) or {}
    return meta.get("snapshot_id") == snapshot_id


def _load_payload(record, *, workspace) -> dict:
    """Read an alias-artifact payload from the registry record.

    Two persistence shapes accepted:

      1. Inline — ``record.metadata["payload"]`` carries the dict.
         Cheapest / preferred for the small payloads alias
         artifacts produce.
      2. On-disk — ``record.location`` points at a JSON file.
         Workspace must be supplied for this path. Returns ``{}``
         when workspace isn't wired or the file is missing.

    Forgiving — any read failure is logged at debug and yields
    an empty dict so the loader skips the bad record."""
    meta = getattr(record, "metadata", None) or {}
    inline = meta.get("payload")
    if isinstance(inline, dict):
        return inline
    if workspace is None:
        return {}
    try:
        runtime_root = workspace.runtime(record.project)
        path = runtime_root / record.location
        if not path.is_file():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _log.debug(
            "alias artifact %s could not be read from disk",
            getattr(record, "artifact_id", "?"),
        )
        return {}
