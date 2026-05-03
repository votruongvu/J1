import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from j1.errors.exceptions import ConfigError
from j1.integration.security.authenticator import ApiKeyRecord

ENV_AUTH_REQUIRED = "J1_AUTH_REQUIRED"
ENV_AUTH_API_KEYS = "J1_AUTH_API_KEYS"
ENV_AUTH_API_KEYS_FILE = "J1_AUTH_API_KEYS_FILE"
ENV_AUTH_JWT_ENABLED = "J1_AUTH_JWT_ENABLED"
ENV_AUTH_ANONYMOUS_PATHS = "J1_AUTH_ANONYMOUS_PATHS"
ENV_AUTH_DEFAULT_TENANT_ID = "J1_AUTH_DEFAULT_TENANT_ID"

DEFAULT_ANONYMOUS_PATHS: frozenset[str] = frozenset({"/health", "/version"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class SecuritySettings:
    """Security configuration loaded from the environment.

    `api_keys` is the **already-resolved** map of token → record. Callers
    keep secrets out of code by pointing `J1_AUTH_API_KEYS_FILE` at a
    secrets-managed JSON file (or by injecting `api_keys` programmatically).
    """

    auth_required: bool = False
    api_keys: Mapping[str, ApiKeyRecord] = field(default_factory=dict)
    jwt_enabled: bool = False
    anonymous_paths: frozenset[str] = field(default_factory=lambda: DEFAULT_ANONYMOUS_PATHS)
    default_tenant_id: str | None = None


def load_security_settings(
    env: Mapping[str, str] | None = None,
) -> SecuritySettings:
    source = env if env is not None else os.environ

    auth_required = source.get(ENV_AUTH_REQUIRED, "").lower() in _TRUTHY
    jwt_enabled = source.get(ENV_AUTH_JWT_ENABLED, "").lower() in _TRUTHY

    anonymous_raw = source.get(ENV_AUTH_ANONYMOUS_PATHS, "")
    anonymous_paths = (
        frozenset(p.strip() for p in anonymous_raw.split(",") if p.strip())
        if anonymous_raw
        else DEFAULT_ANONYMOUS_PATHS
    )

    api_keys = _load_api_keys(source)

    return SecuritySettings(
        auth_required=auth_required,
        api_keys=api_keys,
        jwt_enabled=jwt_enabled,
        anonymous_paths=anonymous_paths,
        default_tenant_id=source.get(ENV_AUTH_DEFAULT_TENANT_ID) or None,
    )


def _load_api_keys(source: Mapping[str, str]) -> Mapping[str, ApiKeyRecord]:
    inline = source.get(ENV_AUTH_API_KEYS)
    file_path = source.get(ENV_AUTH_API_KEYS_FILE)
    if inline and file_path:
        raise ConfigError(
            f"set only one of {ENV_AUTH_API_KEYS} or {ENV_AUTH_API_KEYS_FILE}"
        )
    raw: str | None = None
    if inline:
        raw = inline
    elif file_path:
        try:
            raw = Path(file_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(
                f"failed to read api keys file {file_path!r}: {exc}"
            ) from exc
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"api keys must be valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("api keys JSON must be an object keyed by token")
    return _records_from_json(data)


def _records_from_json(data: Mapping[str, object]) -> dict[str, ApiKeyRecord]:
    records: dict[str, ApiKeyRecord] = {}
    for token, raw_record in data.items():
        if not isinstance(raw_record, Mapping):
            raise ConfigError(
                f"api key {token!r} entry must be an object"
            )
        subject = raw_record.get("subject")
        if not isinstance(subject, str) or not subject:
            raise ConfigError(
                f"api key {token!r} is missing a non-empty 'subject'"
            )
        tenant_id = raw_record.get("tenant_id")
        if tenant_id is not None and not isinstance(tenant_id, str):
            raise ConfigError(
                f"api key {token!r} 'tenant_id' must be a string or null"
            )
        scopes_raw = raw_record.get("scopes", [])
        if not isinstance(scopes_raw, list) or not all(
            isinstance(s, str) for s in scopes_raw
        ):
            raise ConfigError(
                f"api key {token!r} 'scopes' must be a list of strings"
            )
        metadata_raw = raw_record.get("metadata", {})
        if not isinstance(metadata_raw, Mapping):
            raise ConfigError(
                f"api key {token!r} 'metadata' must be an object"
            )
        records[token] = ApiKeyRecord(
            subject=subject,
            tenant_id=tenant_id,
            scopes=frozenset(scopes_raw),
            metadata={str(k): str(v) for k, v in metadata_raw.items()},
        )
    return records
