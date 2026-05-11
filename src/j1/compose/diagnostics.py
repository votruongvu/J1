"""Startup diagnostics — what's wired, never-leaks-secrets.

The composition root produces one `StartupDiagnostics` instance. The
deployment entrypoint logs / exposes it however it wants
(`logging.info`, a `/diagnostics` endpoint, etc.). This module owns
the discipline of "no secrets, no full document content".
"""

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProviderDiagnostics:
    """Per-role / per-provider summary."""

    name: str
    available: bool
    detail: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StartupDiagnostics:
    """Aggregate snapshot the entrypoint logs at startup.

 Every field is opt-in safe — `provider`, `model`, `dimension`,
 counts, names. NEVER includes API keys / base URLs / config dicts.
 """

    compiler_providers: tuple[ProviderDiagnostics, ...] = ()
    graph_providers: tuple[ProviderDiagnostics, ...] = ()
    retrieval_providers: tuple[ProviderDiagnostics, ...] = ()
    enrichment_providers: tuple[ProviderDiagnostics, ...] = ()
    selected_compiler: str | None = None
    selected_graph: str | None = None
    selected_retrieval: str | None = None
    llm_roles: Mapping[str, Mapping[str, str | None]] = field(default_factory=dict)
    enrichment_enabled: bool = False
    enrichment_modalities: tuple[str, ...] = ()
    graphify_enabled: bool = False


def render_startup_diagnostics(diagnostics: StartupDiagnostics) -> list[str]:
    """Format the diagnostics as a list of single-line log statements.

 Returned as separate lines so a deployment can iterate and call
 `logger.info(line)` per line, keeping each line below typical
 log-aggregator size limits.
 """
    lines: list[str] = ["J1 startup diagnostics:"]

    def _registry_line(label: str, providers: tuple[ProviderDiagnostics, ...]) -> str:
        if not providers:
            return f"  {label}: (none registered)"
        names = ", ".join(
            f"{p.name}{'' if p.available else ' [unavailable]'}" for p in providers
        )
        return f"  {label}: {names}"

    lines.append(_registry_line("compilers", diagnostics.compiler_providers))
    lines.append(_registry_line("graph providers", diagnostics.graph_providers))
    lines.append(_registry_line("retrieval providers", diagnostics.retrieval_providers))
    lines.append(_registry_line("enrichment providers", diagnostics.enrichment_providers))
    lines.append(f"  selected compiler: {diagnostics.selected_compiler or '(none)'}")
    lines.append(f"  selected graph: {diagnostics.selected_graph or '(none)'}")
    lines.append(f"  selected retrieval: {diagnostics.selected_retrieval or '(none)'}")
    lines.append(
        f"  enrichment: {'enabled' if diagnostics.enrichment_enabled else 'disabled'}"
        f" modalities=[{', '.join(diagnostics.enrichment_modalities)}]"
    )
    lines.append(
        f"  graphify: {'enabled' if diagnostics.graphify_enabled else 'disabled'}"
    )
    if diagnostics.llm_roles:
        for role, meta in sorted(diagnostics.llm_roles.items()):
            provider = meta.get("provider") or "(unset)"
            model = meta.get("model") or "(unset)"
            extras = []
            if meta.get("dimension"):
                extras.append(f"dim={meta['dimension']}")
            extras_str = (" " + " ".join(extras)) if extras else ""
            lines.append(f"  llm[{role}]: provider={provider} model={model}{extras_str}")
    else:
        lines.append("  llm: (no roles configured)")
    return lines
