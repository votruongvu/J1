"""CitationBinder — separate retrieved / selected / cited evidence
and produce the final citation list.

The legacy bug: the validation response carried 20 citations even
when the LLM only used 4 blocks. Citations were the retrieved
candidate set rather than the cited subset.

The binder enforces ``cited ⊆ selected``:

  * **retrieved** — candidates returned by routes (lives in
    ``QueryTrace.all_candidates``)
  * **selected** — blocks the EvidencePackBuilder chose for synthesis
    (lives in ``QueryTrace.selected``)
  * **cited** — blocks the synthesizer's output actually drew from
    (lives in ``QueryTrace.citations``)

The binder rejects any citation index outside the selected pack —
the synthesizer can't cite a block it wasn't given — so the
contract holds even if the LLM hallucinates indices.
"""

from __future__ import annotations

from j1.query.answer_synthesizer import SynthesisOutput
from j1.query.query_plan import EvidenceBlock


class CitationBinder:
    """Pure: ``bind(selected, output) -> tuple[EvidenceBlock, ...]``."""

    def bind(
        self,
        selected: tuple[EvidenceBlock, ...],
        output: SynthesisOutput,
    ) -> tuple[EvidenceBlock, ...]:
        """Return the subset of ``selected`` the LLM cited.

        ``used_block_indices`` is 0-indexed into ``selected``. Out-
        of-range indices are silently dropped (already filtered by
        the synthesizer, but defence-in-depth never hurt anyone).
        """
        if not output.used_block_indices:
            return ()
        cited: list[EvidenceBlock] = []
        seen: set[tuple[str, str | None]] = set()
        for idx in output.used_block_indices:
            if idx < 0 or idx >= len(selected):
                continue
            block = selected[idx]
            key = (
                block.candidate.artifact_id,
                block.candidate.chunk_id,
            )
            if key in seen:
                continue
            seen.add(key)
            cited.append(block)
        return tuple(cited)


__all__ = ["CitationBinder"]
