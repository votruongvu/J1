"""Capability registry for J1 extension adapters.

Maps `(type, name)` to a registered adapter, plus secondary indexes
on `capability` (from the manifest) and `role` (the workflow role
the adapter is wired to). Adapter instances are not constructed
here — the registry stores the *constructed* instance plus its
`AdapterManifest`. Wiring (resolving secrets, calling `from_default`,
etc.) is the deployment's job.

Design choices:

  * **Local / static registration only** — no plugin discovery, no
    entry-point scanning. A deployment registers its adapters
    explicitly. This keeps the surface small and testable; nothing
    prevents a future plugin loader from being layered on top.
  * **No threading lock by default** — the registry is intended to
    be populated at composition time and read at workflow time.
    Concurrent registration after the worker has started is unusual
    and outside scope.
  * **Quiet on lookup miss; loud on duplicate registration** — the
    common case is "did anyone register a `foo`?" → caller decides.
    The unusual case is "two adapters registered as `foo`" → fail
    fast.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from j1.extension.manifest import AdapterManifest, ManifestError


class RegistryError(LookupError):
    """Raised when registry operations fail (duplicate / type mismatch)."""


@dataclass(frozen=True)
class RegistryEntry:
    """One registered adapter plus its manifest and (optional) role."""

    manifest: AdapterManifest
    adapter: Any
    role: str | None = None


class CapabilityRegistry:
    """Indexes registered adapters by type, name, capability, and role.

    Lookups:
      * `get(type, name)` — exact lookup; returns the entry or `None`.
      * `find_by_type(type)` — every entry of this type.
      * `find_by_capability(capability)` — every entry that declares
        this capability in its manifest.
      * `find_by_role(role)` — every entry wired to this workflow role.

    Mutations:
      * `register(manifest, adapter, *, role=None)` — adds the entry.
        Duplicate `(type, name)` raises `RegistryError`.
      * `unregister(type, name)` — removes the entry. No-op if absent.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], RegistryEntry] = {}
        self._by_type: dict[str, list[RegistryEntry]] = {}
        self._by_capability: dict[str, list[RegistryEntry]] = {}
        self._by_role: dict[str, list[RegistryEntry]] = {}

    # ---- Mutations --------------------------------------------------

    def register(
        self,
        manifest: AdapterManifest,
        adapter: Any,
        *,
        role: str | None = None,
    ) -> RegistryEntry:
        """Register one adapter. Returns the created `RegistryEntry`."""
        if not isinstance(manifest, AdapterManifest):
            raise ManifestError(
                f"register() requires AdapterManifest, got {type(manifest).__name__}"
            )
        key = (manifest.type, manifest.name)
        if key in self._entries:
            raise RegistryError(
                f"adapter already registered: type={manifest.type!r} "
                f"name={manifest.name!r}"
            )
        # Light type-coherence check: if the adapter has a `kind`
        # attribute and it disagrees with the manifest name, that's a
        # confusing combination — fail fast instead of letting the
        # registry index two different identities.
        adapter_kind = getattr(adapter, "kind", None)
        if isinstance(adapter_kind, str) and adapter_kind != manifest.name:
            raise RegistryError(
                f"adapter.kind={adapter_kind!r} disagrees with "
                f"manifest.name={manifest.name!r}"
            )
        entry = RegistryEntry(manifest=manifest, adapter=adapter, role=role)
        self._entries[key] = entry
        self._by_type.setdefault(manifest.type, []).append(entry)
        for capability in manifest.capabilities:
            self._by_capability.setdefault(capability, []).append(entry)
        if role is not None:
            self._by_role.setdefault(role, []).append(entry)
        return entry

    def unregister(self, adapter_type: str, name: str) -> None:
        """Remove an entry. Silent no-op if it wasn't registered."""
        key = (adapter_type, name)
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        _drop(self._by_type.get(adapter_type), entry)
        for capability in entry.manifest.capabilities:
            _drop(self._by_capability.get(capability), entry)
        if entry.role is not None:
            _drop(self._by_role.get(entry.role), entry)

    # ---- Lookups ----------------------------------------------------

    def get(self, adapter_type: str, name: str) -> RegistryEntry | None:
        return self._entries.get((adapter_type, name))

    def require(self, adapter_type: str, name: str) -> RegistryEntry:
        """Like `get()`, but raises `RegistryError` if absent."""
        entry = self.get(adapter_type, name)
        if entry is None:
            raise RegistryError(
                f"no adapter registered: type={adapter_type!r} name={name!r}"
            )
        return entry

    def find_by_type(self, adapter_type: str) -> list[RegistryEntry]:
        return list(self._by_type.get(adapter_type, ()))

    def find_by_capability(self, capability: str) -> list[RegistryEntry]:
        return list(self._by_capability.get(capability, ()))

    def find_by_role(self, role: str) -> list[RegistryEntry]:
        return list(self._by_role.get(role, ()))

    def __iter__(self) -> Iterator[RegistryEntry]:
        return iter(self._entries.values())

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        if isinstance(key, tuple) and len(key) == 2:
            return key in self._entries
        return False

    # ---- Diagnostics ------------------------------------------------

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a JSON-friendly summary of every registered adapter.

        Useful for `/capabilities`-style endpoints, structured
        logging at startup, and tests.
        """
        return [
            {
                "manifest": entry.manifest.to_dict(),
                "role": entry.role,
                "adapter_class": type(entry.adapter).__name__,
            }
            for entry in self._entries.values()
        ]


def _drop(bucket: list | None, entry: RegistryEntry) -> None:
    if bucket is None:
        return
    try:
        bucket.remove(entry)
    except ValueError:
        return


__all__ = [
    "CapabilityRegistry",
    "RegistryEntry",
    "RegistryError",
]
