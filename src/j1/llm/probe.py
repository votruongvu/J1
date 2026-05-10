"""Startup connectivity probe for the LLM provider stack.

Why: when J1 ingests a document the worker calls the LLM many times
(planner, entity extraction, vision captioning, embeddings). If the
LLM endpoint is unreachable at run time, the run crashes mid-flight,
the user uploads a document, sees a generic failure, and the operator
has to dig through logs to find the cause.

This module exercises every CONFIGURED LLM role at startup with a
tiny, idempotent request. If any role's probe fails, the caller is
expected to abort process startup (FastAPI lifespan / worker main)
with a clear, copy-paste-able error message naming the unreachable
endpoint.

The probe is opt-out via `J1_LLM_STARTUP_PROBE=false` for tests and
mock-only deployments — production runs leave it enabled.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from j1.llm.errors import LLMProviderUnavailable
from j1.llm.registry import (
    LLM_ROLE_EMBEDDING,
    LLM_ROLE_FAST,
    LLM_ROLE_PREMIUM,
    LLM_ROLE_TEXT,
    LLM_ROLE_VISION,
    LLMProviderRegistry,
)

_log = logging.getLogger("j1.llm.probe")

ENV_LLM_STARTUP_PROBE = "J1_LLM_STARTUP_PROBE"

# Roles probed when present. Order matters only for deterministic log
# output. Each role uses the cheapest possible API call to confirm
# connectivity + auth + model availability.
#
# The TEXT and FAST roles share a generate() probe with a 1-token
# response cap. EMBEDDING uses a 1-character embed_text() call.
# VISION + PREMIUM are intentionally NOT probed — vision needs a real
# image payload (no idempotent zero-cost shape) and PREMIUM is
# expensive per call. Both fail loudly at use-time if misconfigured;
# the startup probe focuses on the always-on path.
_PROBED_ROLES: tuple[str, ...] = (
    LLM_ROLE_TEXT,
    LLM_ROLE_FAST,
    LLM_ROLE_EMBEDDING,
)


@dataclass(frozen=True)
class ProbeResult:
    role: str
    ok: bool
    provider: str | None
    model: str | None
    error: str | None = None


class LLMStartupProbeError(RuntimeError):
    """Raised when one or more required LLM roles are unreachable.

    Caller catches this and aborts startup with the error's message
    rendered to stderr. The message is operator-facing — names the
    role, provider, model, base URL when available, and the wrapped
    error string."""


def llm_probe_enabled(env: dict | None = None) -> bool:
    """Returns True when the startup probe should run.

    Defaults to True. Set `J1_LLM_STARTUP_PROBE=false` to opt out
    (tests and mock-only deployments)."""
    source = env if env is not None else os.environ
    raw = str(source.get(ENV_LLM_STARTUP_PROBE, "true")).strip().lower()
    return raw not in {"false", "0", "no", "off"}


def probe_registry(
    registry: LLMProviderRegistry,
    *,
    roles: tuple[str, ...] | None = None,
) -> list[ProbeResult]:
    """Exercise each configured role with a minimal request.

    Returns a `ProbeResult` per probed role. Roles that aren't
    registered are skipped silently — the operator opted not to
    configure them. Roles that ARE registered but fail the probe
    return `ok=False` with the error string.

    Caller decides how to handle failures (abort startup, log + warn,
    etc.). This function never raises for a probe failure — it
    returns the result so the caller controls the policy.
    """
    targets = roles if roles is not None else _PROBED_ROLES
    results: list[ProbeResult] = []
    for role in targets:
        client = registry.try_resolve(role)
        if client is None:
            continue
        provider = getattr(client, "provider", None)
        model = getattr(client, "model", None)
        try:
            _exercise_role(role, client)
            results.append(ProbeResult(
                role=role, ok=True, provider=provider, model=model,
            ))
        except Exception as exc:  # noqa: BLE001 — probe converts every error
            results.append(ProbeResult(
                role=role, ok=False, provider=provider, model=model,
                error=f"{type(exc).__name__}: {exc}",
            ))
    return results


def assert_required_llm_reachable(
    registry: LLMProviderRegistry,
    *,
    roles: tuple[str, ...] | None = None,
) -> None:
    """Run the probe; raise `LLMStartupProbeError` on any failure.

    Convenience wrapper for the common case: 'fail startup if any
    configured-and-required role can't be reached.' Logs each result
    at INFO so the startup banner shows the LLM diagnostic state."""
    results = probe_registry(registry, roles=roles)
    if not results:
        _log.info(
            "LLM startup probe: no probed roles registered; skipping",
        )
        return
    failures: list[ProbeResult] = []
    for result in results:
        if result.ok:
            _log.info(
                "LLM probe ok: role=%s provider=%s model=%s",
                result.role, result.provider, result.model,
            )
        else:
            _log.error(
                "LLM probe FAILED: role=%s provider=%s model=%s error=%s",
                result.role, result.provider, result.model, result.error,
            )
            failures.append(result)
    if failures:
        lines = [
            "J1 cannot start: one or more configured LLM roles are "
            "unreachable. Fix the endpoint(s) below and restart.",
            "",
        ]
        for f in failures:
            lines.append(
                f"  - role={f.role}  provider={f.provider}  "
                f"model={f.model}\n      {f.error}"
            )
        lines.append("")
        lines.append(
            "Hints: confirm the LM Studio / vLLM / hosted endpoint is "
            "running, the API key is correct, and the model is loaded. "
            "Set J1_LLM_STARTUP_PROBE=false to bypass this check (NOT "
            "recommended for production — runs will fail mid-pipeline "
            "instead of at startup)."
        )
        raise LLMStartupProbeError("\n".join(lines))


# ---- Per-role exercise paths ---------------------------------------


def _exercise_role(role: str, client: object) -> None:
    """One-shot, idempotent probe for the given role.

    Each probe is the cheapest-possible call that confirms
    (a) network reachability, (b) auth, (c) model availability.
    On success, returns None. On any failure (timeout, HTTP error,
    auth error, model-not-loaded), raises so the caller's
    `try/except` collects the failure.
    """
    if role == LLM_ROLE_EMBEDDING:
        embed = getattr(client, "embed_text", None)
        if embed is None:
            raise LLMProviderUnavailable(
                f"embedding client {type(client).__name__} has no embed_text"
            )
        # Single-char input is the smallest valid embedding payload —
        # most providers tokenise it to one token.
        embed("a")
        return

    # TEXT / FAST / PREMIUM all share TextLLMClient.generate.
    generate = getattr(client, "generate", None)
    if generate is None:
        raise LLMProviderUnavailable(
            f"text client {type(client).__name__} has no generate"
        )
    # Cap to 1 token so the probe doesn't burn budget. Any prompt
    # the model considers valid works; "ping" is short + recognisable
    # in provider logs.
    generate("ping", max_output_tokens=1, temperature=0.0)
