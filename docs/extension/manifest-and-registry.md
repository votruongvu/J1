# Adapter Manifests and the Capability Registry

How J1 indexes, looks up, and validates the adapters /
connectors / providers / domain policies a deployment registers.

The two pieces:

| Piece | Purpose |
|---|---|
| [`AdapterManifest`](../../src/j1/extension/manifest.py) | Plain-dataclass schema describing one adapter (name, type, capabilities, required config + secret keys, …). |
| [`CapabilityRegistry`](../../src/j1/extension/registry.py) | In-memory index that maps `(type, name)` → `(manifest, adapter, role)` plus secondary indexes on `capability` and `role`. |

---

## 1. The `AdapterManifest` schema

```python
@dataclass(frozen=True)
class AdapterManifest:
 name: str # e.g. "acme.compiler"
 type: str # one of KNOWN_ADAPTER_TYPES (or "unknown:foo")
 version: str # MAJOR[.MINOR[.PATCH]][-prerelease]
 capabilities: tuple[str,...] = # free-form labels
 supported_input_types: tuple[str,...] = 
 output_types: tuple[str,...] = 
 required_config_keys: tuple[str,...] = 
 optional_config_keys: tuple[str,...] = 
 required_secret_keys: tuple[str,...] = # NAMES, not values
 health_check: bool = False
 description: str | None = None
 metadata: dict[str, Any] = {}
```

### Field rules

- `name` — lowercase ASCII + digits + `.`, `-`, `_`. Vendors MUST
 namespace (e.g. `acme.compiler`) to avoid clashes with bundled
 adapters whose names are short (`mock`, `raganything`, …).
- `type` — one of `KNOWN_ADAPTER_TYPES` (`source-connector`,
 `compiler`, `enrichment`, `graph`, `retrieval`, `reranker`, `llm`,
 `embedding`, `vision`, `output-formatter`, `evaluation`,
 `domain-policy`). Experimental types use `unknown:<your-name>`.
- `version` — `1`, `1.0`, `1.0.0`, optionally suffixed with
 `-rc.1` / `+build.5`.
- `required_config_keys` and `optional_config_keys` MUST NOT
 overlap (the constructor raises `ManifestError` otherwise).
- `required_secret_keys` lists the *names* of secrets the adapter
 expects. The manifest never carries secret values; resolution is
 the deployment's job.

### Secret-shape guard

`AdapterManifest` runs a best-effort heuristic against
`metadata.values` and rejects values that look like API keys
(`sk-…`, `ghp_…`, `xoxb-…`, `AKIA…`, long opaque tokens). It is
not a security boundary — it just catches the common mistake of
pasting a token into the manifest.

### Round-trip

```python
m = AdapterManifest(name="acme.retrieval", type="retrieval", version="1.0.0")
m_dict = m.to_dict
restored = AdapterManifest.from_dict(m_dict)
assert restored == m
```

`from_dict` is tolerant (missing optional keys → defaults) but
strict on required ones. Useful for loading manifests from YAML /
JSON config files.

---

## 2. Bundled mock manifests

Each mock under [`j1.extension.mocks`](../../src/j1/extension/mocks.py)
exposes a `MANIFEST` class attribute. Use them as templates:

```python
MANIFEST = AdapterManifest(
 name="mock",
 type="compiler",
 version="0.1.0",
 capabilities=("text",),
 output_types=("compiled.text",),
 description="In-memory compiler that produces one draft per document.",
)
```

---

## 3. The `CapabilityRegistry`

```python
from j1.extension import CapabilityRegistry

reg = CapabilityRegistry
adapter = AcmeCompiler(...)
reg.register(adapter.MANIFEST, adapter, role="primary-compile")

# Lookups
reg.get("compiler", "acme.compiler") # exact (or None)
reg.require("compiler", "acme.compiler") # exact (raises if missing)
reg.find_by_type("compiler") # all of this type
reg.find_by_capability("multilingual") # all with this capability
reg.find_by_role("primary-compile") # all wired to this role
```

### Registration rules

- **Duplicate `(type, name)`** → `RegistryError`. Vendors whose
 manifest names collide with another vendor MUST namespace the
 conflict away.
- **`adapter.kind` MUST equal `manifest.name`** (when both are
 set). Disagreement raises — the registry refuses to index two
 identities for one adapter.
- **`role` is optional.** When set, the entry is also indexed under
 that role. Use roles to wire workflow steps (`"primary-retrieve"`,
 `"fallback-retrieve"`, …) without coupling to a specific adapter
 name.

### Diagnostics

```python
reg.snapshot
# → [{"manifest": {…}, "role": "primary-compile",
# "adapter_class": "AcmeCompiler"}, …]
```

Suitable for `/capabilities`-style endpoints, structured-log boot
diagnostics, or test introspection.

### Scope

The registry is **local / static**. There is no plugin discovery,
no entry-point scanning, no hot-reload. A deployment registers
its adapters explicitly at composition time. This is intentional:

- Tests can build small registries deterministically.
- The composition root is the only place that knows what's wired.
- A future plugin loader can layer on top without changing the
 registry contract.

There is also no thread lock around mutations. The registry is
populated at startup and read at workflow time; concurrent
registration after the worker has started is unusual and out of
scope.

---

## 4. End-to-end example

```python
from j1.extension import (
 AdapterManifest, CapabilityRegistry, ProjectContext,
)
from acme_pkg.compiler import AcmeCompiler
from acme_pkg.retrieval import AcmeRetrieval

reg = CapabilityRegistry

reg.register(
 AdapterManifest(
 name="acme.compiler", type="compiler", version="1.0.0",
 capabilities=("text", "pdf"),
 required_secret_keys=("ACME_API_KEY",),
 ),
 AcmeCompiler(api_key=resolve_secret("ACME_API_KEY")),
 role="primary-compile",
)

reg.register(
 AcmeRetrieval.MANIFEST,
 AcmeRetrieval(...),
 role="primary-retrieve",
)

# Workflow step
ctx = ProjectContext(tenant_id="acme", project_id="alpha")
compiler = reg.require("compiler", "acme.compiler").adapter
result = compiler.compile(ctx, document_id="doc-1")
```

---

## 5. Cross-references

- [`docs/extension/contracts.md`](contracts.md) — the 12 contracts
 that adapters implement
- [`docs/extension/conformance-tests.md`](conformance-tests.md) —
 validating an adapter implements its contract correctly
- [`docs/extension/add-a-provider.md`](add-a-provider.md) — full
 provider recipe (settings, bridge, registration)
- [`src/j1/extension/manifest.py`](../../src/j1/extension/manifest.py)
- [`src/j1/extension/registry.py`](../../src/j1/extension/registry.py)
- [`tests/extension/test_manifest.py`](../../tests/extension/test_manifest.py)
- [`tests/extension/test_registry.py`](../../tests/extension/test_registry.py)
