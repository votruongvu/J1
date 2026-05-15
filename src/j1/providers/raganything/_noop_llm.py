"""No-op `llm_model_func` for the `minimum_queryable` execution profile.

LightRAG's `apipeline_process_enqueue_documents` (called by
`RAGAnything.process_document_complete` and the bridge's fast-path
`lightrag.ainsert`) is documented to "write chunks to in-memory
storage in stage 1, then run LLM-driven entity extraction in
stage 2" — see [_bridge.py:1754](./_bridge.py#L1754). The library
exposes no `disable_entity_extraction` flag; the only J1-controlled
boundary is the `llm_model_func` callable passed at construction.

For the `minimum_queryable` execution profile we swap the real
text-LLM callable for the one defined here. It returns LightRAG's
"no entities found" sentinel (just the completion delimiter) so
the parser produces empty `maybe_nodes` / `maybe_edges` immediately
and pipeline-stage 2 completes without firing any LLM tokens. The
chunks + embeddings persist via `_force_persist_chunks` and the
document is queryable via vector retrieval.

This is the keystone enabling an honestly-minimal ingest path
without forking LightRAG. See
[docs/11-ingestion-execution-profiles.md](../../../../docs/11-ingestion-execution-profiles.md)
for the full investigation.

Design rules:

 1. **Async** — LightRAG `await`s the callable; the wrapper must
    be `async` even though it does no I/O.
 2. **Signature parity** — the callable accepts the same kwargs
    as the real one (`prompt`, `system_prompt`, `history_messages`,
    plus `**kwargs` for vendor-version drift).
 3. **Telemetry on every call** — every short-circuit is logged
    with `purpose=entity_extraction_noop_minimum_queryable` and a
    per-instance call counter, so the heavy-operation-detected
    audit event can prove the no-op is actually being hit instead
    of a real LLM call slipping through.
 4. **Default-delimiter sentinel** — returns LightRAG's
    `<|COMPLETE|>` constant. The bridge config never overrides
    `tuple_delimiter` / `completion_delimiter`, so this matches
    the parser's expectations and produces a clean "no entities,
    no warnings" parse.

Used ONLY from the compile-side bridge factory
(`_build_rag_instance`). Query-side bridge factory
(`_build_rag_instance_with_lightrag_for_query`) is unaffected —
queries still call the real LLM.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any


_log = logging.getLogger(__name__)


# LightRAG's default completion delimiter. Hard-coded here so the
# no-op produces a clean parse without importing the vendor module
# at module-load time (the bridge already keeps the import lazy
# via `_import_raganything`). Pinned in
# `lightrag/prompt.py::PROMPTS["DEFAULT_COMPLETION_DELIMITER"]`;
# a vendor change here would surface as a warning in the LightRAG
# logs ("Complete delimiter can not be found in extraction result")
# rather than a hang, so the failure mode is bounded.
_LIGHTRAG_COMPLETION_DELIMITER = "<|COMPLETE|>"


def make_noop_text_callable(
    *,
    on_call: Callable[[dict[str, Any]], None] | None = None,
) -> Callable[..., Any]:
    """Build an async `llm_model_func` that short-circuits to "no
    entities found" and never calls a real LLM.

    The returned callable counts invocations on its own attribute
    (`call_count`) so a test or the workflow-side audit hook can
    assert "the no-op fired N times, the real LLM fired 0 times".

    `on_call`, when supplied, is invoked synchronously inside the
    callable with `{"prompt_preview", "system_prompt_preview",
    "history_messages_count"}` — a deliberate-narrow surface so
    audit hooks can record "purpose=entity_extraction_noop" without
    holding references to large prompt strings. Failures inside
    `on_call` are caught + logged but never raised (audit must
    never break a compile).
    """

    async def _noop_llm(
        prompt: str = "",
        system_prompt: str | None = None,
        history_messages: list | None = None,
        *args,
        **kwargs,
    ) -> str:
        _noop_llm.call_count += 1  # type: ignore[attr-defined]
        if on_call is not None:
            try:
                on_call({
                    "prompt_preview": (prompt[:120] if prompt else ""),
                    "system_prompt_preview": (
                        (system_prompt or "")[:120]
                    ),
                    "history_messages_count": (
                        len(history_messages) if history_messages else 0
                    ),
                })
            except Exception:  # noqa: BLE001 — audit must not fail compile
                _log.warning(
                    "noop-llm on_call hook raised; ignoring",
                    exc_info=True,
                )
        _log.debug(
            "noop-llm short-circuited (entity_extraction_noop_minimum_queryable, "
            "call_count=%d)",
            _noop_llm.call_count,  # type: ignore[attr-defined]
        )
        return _LIGHTRAG_COMPLETION_DELIMITER

    _noop_llm.call_count = 0  # type: ignore[attr-defined]
    return _noop_llm
