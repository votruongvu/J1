"""Build operator-facing warnings about missing diagnostic fields.

The manual-test query endpoint stamps the full ``QueryTrace`` onto
``ManualTestQueryResponseDTO.debug["orchestrator_trace"]``. Operators
investigating a query that returned no answer (or a partial one)
need to know whether a given diagnostic field is absent because the
run *didn't reach the stage that fills it* or because *the
projection dropped it*. Without that distinction every empty field
looks like a bug.

This builder walks the trace dict and emits one short string per
expected-but-missing diagnostic. The output is purely advisory —
the response shape is unaffected, and the warnings are stamped onto
``debug["diagnostic_warnings"]`` for the FE to render as a banner
above the raw-payload drawer.

Contract:

* Never raise. A malformed trace produces a warning, not a crash.
* Always return a list (possibly empty). Empty list = "every
  expected diagnostic is present".
* One warning per field — operators eyeball the list, not page
  through it.
* Messages are short, name the field, and say what's wrong
  ("absent", "empty", "inconsistent").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


__all__ = ["build_diagnostic_warnings"]


# ---- Expected-field contract ------------------------------------


# Top-level trace keys that MUST be present and non-empty in any
# real orchestrator run. An empty container here is the signal that
# the stage that fills it didn't run or its result was dropped.
_REQUIRED_TOP_LEVEL_KEYS: tuple[str, ...] = (
    "snapshot_scope",
    "augmentation",
    "routes_executed",
    "final_status",
)


# Snapshot-scope sub-keys we report on. ``eligible_snapshot_ids`` is
# the load-bearing one — an empty list means the eligibility
# resolver returned nothing, which is the root cause for "no
# eligible snapshot" refusals.
_SNAPSHOT_SCOPE_REQUIRED: tuple[str, ...] = (
    "eligible_snapshot_ids",
)


# Augmentation sub-keys. ``applied_to_retrieval`` must be an
# explicit boolean — its absence means the augmentation provider
# didn't stamp its decision onto the trace, which is a wiring bug
# operators need to see.
_AUGMENTATION_REQUIRED_PRESENCE: tuple[str, ...] = (
    "applied_to_retrieval",
)


def build_diagnostic_warnings(trace_dict: Any) -> list[str]:
    """Return the list of operator-facing warnings for ``trace_dict``.

    ``trace_dict`` is the JSON-shaped output of
    ``QueryTrace.to_dict()``. When the orchestrator path bypassed
    the trace entirely (e.g. native-debug, refusal pre-orchestrator)
    pass ``None`` or an empty dict — the builder emits a single
    "trace absent" warning and returns.
    """
    warnings: list[str] = []

    if not isinstance(trace_dict, Mapping) or not trace_dict:
        warnings.append(
            "orchestrator_trace: absent — orchestrator did not run "
            "or its trace was dropped before projection",
        )
        return warnings

    for key in _REQUIRED_TOP_LEVEL_KEYS:
        if key not in trace_dict:
            warnings.append(
                f"{key}: absent from orchestrator trace",
            )
            continue
        value = trace_dict[key]
        if value in (None, "", [], {}):
            warnings.append(
                f"{key}: present but empty — stage may not have run",
            )

    # `duration_ms` is a single numeric we treat separately: a zero
    # value means the orchestrator never timed the run, which is a
    # wiring bug operators should see (distinct from "ran in <1 ms").
    duration_ms = trace_dict.get("duration_ms")
    if duration_ms is None:
        warnings.append("duration_ms: absent from orchestrator trace")
    elif not isinstance(duration_ms, (int, float)) or duration_ms <= 0:
        warnings.append(
            "duration_ms: present but non-positive — orchestrator "
            "timing seam may be unwired",
        )

    snapshot_scope = trace_dict.get("snapshot_scope")
    if isinstance(snapshot_scope, Mapping):
        for sub in _SNAPSHOT_SCOPE_REQUIRED:
            if sub not in snapshot_scope:
                warnings.append(
                    f"snapshot_scope.{sub}: absent",
                )
                continue
            sub_value = snapshot_scope[sub]
            if not sub_value:
                warnings.append(
                    f"snapshot_scope.{sub}: empty — no eligible "
                    "snapshot resolved for this query",
                )

    augmentation = trace_dict.get("augmentation")
    if isinstance(augmentation, Mapping):
        for sub in _AUGMENTATION_REQUIRED_PRESENCE:
            if sub not in augmentation:
                warnings.append(
                    f"augmentation.{sub}: absent — augmentation "
                    "provider did not stamp its decision onto the "
                    "trace",
                )
        # Only flag empty terms/aliases when the source advertises a
        # non-disabled augmentation — operators running with the flag
        # off shouldn't see noise.
        source = augmentation.get("source") or ""
        if source and source != "disabled":
            terms = augmentation.get("terms") or []
            aliases = augmentation.get("aliases") or []
            expansions = augmentation.get("expansions") or []
            if not terms and not aliases and not expansions:
                warnings.append(
                    f"augmentation: source={source!r} but no terms / "
                    "aliases / expansions captured",
                )

    return warnings
