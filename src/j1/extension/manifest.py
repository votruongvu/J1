"""Adapter / connector / provider manifest schema.

A manifest is a small JSON-friendly description of one extension
implementation. It is used by:

  * the capability registry, which indexes adapters by `type` /
    `name` / `capability` / `role`;
  * deployment-side validation that all required configuration +
    secret keys are wired before the adapter is instantiated;
  * documentation generation (the manifest IS the contract a
    deployment commits to).

Manifests are deliberately:

  * **Plain dataclasses** — no JSON Schema dep, no Pydantic in core.
    Validation is in-Python and surfaces actionable error messages.
  * **Domain-neutral** — `name`, `type`, `capabilities` are opaque
    strings to the framework.
  * **Secret-safe** — `required_secret_keys` lists *names* of secrets
    the adapter expects, never the secrets themselves.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Allowed adapter types. Open set in principle, but a small canonical
# vocabulary makes manifests self-documenting and registry queries
# more useful. Custom types are allowed via `unknown:<your-type>`.
KNOWN_ADAPTER_TYPES: frozenset[str] = frozenset({
    "source-connector",
    "compiler",
    "enrichment",
    "graph",
    "retrieval",
    "reranker",
    "llm",
    "embedding",
    "vision",
    "output-formatter",
    "evaluation",
    "domain-policy",
})

# Identifier shape: lowercase ASCII + digits + dot / dash / underscore.
# Same character class J1 uses for tenant / project ids elsewhere,
# extended with `.` so vendors can namespace (e.g. `mycompany.azure`).
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,2}([-+][a-z0-9._-]+)?$")


class ManifestError(ValueError):
    """Raised when a manifest is structurally invalid.

    Inherits `ValueError` so existing code that catches `ValueError`
    around configuration parsing keeps working.
    """


@dataclass(frozen=True)
class AdapterManifest:
    """Metadata describing one adapter / connector / provider.

    Required:
      * `name` — unique within the registry; vendors MUST namespace
        (e.g. `acme.compiler`).
      * `type` — one of `KNOWN_ADAPTER_TYPES` or `unknown:<…>`.
      * `version` — `MAJOR[.MINOR[.PATCH]][-prerelease]` shape.

    Optional:
      * `capabilities` — free-form labels the registry indexes
        (e.g. `streaming`, `batch`, `multilingual`). Used by
        capability-based lookups.
      * `supported_input_types` / `output_types` — content-type
        labels; the framework does not match on them automatically,
        but they are documented and queryable.
      * `required_config_keys` / `optional_config_keys` — names of
        plaintext config the adapter expects on construction.
      * `required_secret_keys` — names of *secret* config the
        adapter expects (the manifest does NOT carry the secrets).
      * `health_check` — `True` if the adapter implements a
        `health_check(ctx) -> dict` method the registry can call.
      * `description` — human-readable summary.
      * `metadata` — free-form bag for vendor extensions.
    """

    name: str
    type: str
    version: str
    capabilities: tuple[str, ...] = field(default_factory=tuple)
    supported_input_types: tuple[str, ...] = field(default_factory=tuple)
    output_types: tuple[str, ...] = field(default_factory=tuple)
    required_config_keys: tuple[str, ...] = field(default_factory=tuple)
    optional_config_keys: tuple[str, ...] = field(default_factory=tuple)
    required_secret_keys: tuple[str, ...] = field(default_factory=tuple)
    health_check: bool = False
    description: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_name("name", self.name)
        _validate_type(self.type)
        _validate_version(self.version)
        for c in self.capabilities:
            _validate_name("capability", c)
        # Reject overlap between required/optional config keys —
        # ambiguous and a common manifest error.
        overlap = set(self.required_config_keys) & set(self.optional_config_keys)
        if overlap:
            raise ManifestError(
                f"manifest {self.name!r}: keys appear in both required and "
                f"optional config: {sorted(overlap)}"
            )
        # No "secret-shaped" string in the value of `metadata` — best
        # effort, not perfect. Catches the most common mistake of
        # pasting a token into the manifest itself.
        for key, value in self.metadata.items():
            if isinstance(value, str) and _looks_like_secret(value):
                raise ManifestError(
                    f"manifest {self.name!r}: metadata key {key!r} value "
                    f"looks like a secret. Put the secret in the deployment's "
                    f"secret manager and list its name in `required_secret_keys`."
                )

    # ---- Convenience ------------------------------------------------

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict (handy for JSON serialisation / logging)."""
        return {
            "name": self.name,
            "type": self.type,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "supported_input_types": list(self.supported_input_types),
            "output_types": list(self.output_types),
            "required_config_keys": list(self.required_config_keys),
            "optional_config_keys": list(self.optional_config_keys),
            "required_secret_keys": list(self.required_secret_keys),
            "health_check": self.health_check,
            "description": self.description,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdapterManifest":
        """Build a manifest from a plain dict (e.g. parsed YAML / JSON).

        Tolerates missing optional keys; validates everything via
        `__post_init__`.
        """
        if not isinstance(data, dict):
            raise ManifestError(
                f"manifest must be an object, got {type(data).__name__}"
            )
        try:
            return cls(
                name=str(data["name"]),
                type=str(data["type"]),
                version=str(data["version"]),
                capabilities=_as_tuple(data.get("capabilities")),
                supported_input_types=_as_tuple(data.get("supported_input_types")),
                output_types=_as_tuple(data.get("output_types")),
                required_config_keys=_as_tuple(data.get("required_config_keys")),
                optional_config_keys=_as_tuple(data.get("optional_config_keys")),
                required_secret_keys=_as_tuple(data.get("required_secret_keys")),
                health_check=bool(data.get("health_check", False)),
                description=data.get("description"),
                metadata=dict(data.get("metadata") or {}),
            )
        except KeyError as exc:
            raise ManifestError(f"manifest missing required key: {exc.args[0]!r}") from exc


# ---- Validation primitives -----------------------------------------


def _validate_name(field: str, value: object) -> None:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise ManifestError(
            f"manifest {field}={value!r} must match {_NAME_RE.pattern!r} "
            f"(lowercase ASCII letters / digits / '.' '-' '_')"
        )


def _validate_type(value: object) -> None:
    if not isinstance(value, str):
        raise ManifestError(f"manifest type must be a string, got {type(value).__name__}")
    if value in KNOWN_ADAPTER_TYPES:
        return
    if value.startswith("unknown:"):
        suffix = value[len("unknown:"):]
        _validate_name("type suffix", suffix)
        return
    raise ManifestError(
        f"manifest type {value!r} is not a known adapter type. "
        f"Known types: {sorted(KNOWN_ADAPTER_TYPES)}. "
        f"For experimental types use `unknown:<your-name>`."
    )


def _validate_version(value: object) -> None:
    if not isinstance(value, str) or not _VERSION_RE.match(value):
        raise ManifestError(
            f"manifest version {value!r} must match {_VERSION_RE.pattern!r}"
        )


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        # Single string, not a list — accept gracefully.
        return (value,)
    if isinstance(value, Iterable):
        return tuple(str(v) for v in value)
    raise ManifestError(f"expected a list of strings, got {type(value).__name__}")


# Best-effort heuristic: catches obvious accidental secret pastes.
# Not a security boundary — the source of truth is "secrets live in
# `required_secret_keys` and are resolved by the deployment".
_SECRET_PREFIXES = ("sk-", "ghp_", "xoxb-", "AKIA", "ya29.")


def _looks_like_secret(value: str) -> bool:
    if any(value.startswith(p) for p in _SECRET_PREFIXES):
        return True
    # Long opaque token: 40+ chars of alphanumerics/underscores.
    if len(value) >= 40 and re.fullmatch(r"[A-Za-z0-9_\-]+", value):
        return True
    return False


__all__ = [
    "AdapterManifest",
    "KNOWN_ADAPTER_TYPES",
    "ManifestError",
]
