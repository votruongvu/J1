# Contributing to J1

Welcome. J1 is a reusable, domain-neutral Python framework for
knowledge-document processing. This document captures the rules a
contribution must satisfy before it can land.

If you're new to the codebase, start with
[`docs/development/onboarding.md`](docs/development/onboarding.md)
— it walks you from install to first workflow run. Then read this
file before opening a PR.

---

## 1. Architecture boundaries

The framework is organised in concentric layers; the dependency arrow
points strictly outward → inward (outer layers depend on inner;
never the reverse):

```
adapters (REST, webhook, …)        ← outer, transport-specific
  ↓
integration (ports, DTOs, services, security, events, bulk)
  ↓
core (intake, processing, query, search, artifacts, audit, cost,
      review, connectors, enrichers, orchestration, llm, providers)
```

Tests in [`tests/test_integration_layer.py`](tests/test_integration_layer.py)
and [`tests/test_external_integration_consistency.py`](tests/test_external_integration_consistency.py)
enforce this AST-level. A drift fails the build.

For the full layer rules + the rationale, see
[`docs/external-integration-architecture.md`](docs/external-integration-architecture.md).

---

## 2. Domain-neutral core rule

J1 must remain reusable across any document-intelligence workload.
Therefore:

- **No industry vocabulary** anywhere in `src/j1/` outside
  `src/j1/profiles/`. No civil / legal / medical / clinical /
  financial / training / customer-name strings, identifiers, or
  comments.
- **No phase-based names** (`phase1`, `phrase2`, `step3`, "training
  phase", etc.) used as core concepts.
- **No customer-tenant assumptions** — the `default` tenant is
  configurable, but never hardcoded.

Domain-specific work belongs in your own package — see
[`docs/extension/domain-module-isolation.md`](docs/extension/domain-module-isolation.md)
for the recommended layout.

---

## 3. Provider / adapter rules

Vendor SDKs and external tools live behind providers. They must:

- **Be lazy-imported.** Top-level `import vendor_sdk` is forbidden in
  any module that imports cleanly without the optional dependency.
- **Be isolated to `src/j1/providers/<name>/`, `src/j1/llm/<vendor>.py`,
  or `src/j1/adapters/<name>/`.** Core processing modules must not
  import vendor SDKs.
- **Never leak vendor objects past their boundary.** Provider
  functions return canonical J1 types
  (`ArtifactDraft`, `ArtifactProcessingResult`, `QueryResult`,
  `(text, TokenUsage)`, etc.). Vendor classes stay inside the
  provider module.
- **Translate exceptions at the boundary.** `ProviderUnavailable` for
  actionable infra failures; `ArtifactProcessingResult(status=FAILED)`
  for per-call failures.
- **Provide a test seam.** Constructors take an injectable callable;
  tests use it; `from_default(...)` wires the production bridge.

For the full recipe see
[`docs/extension/add-a-provider.md`](docs/extension/add-a-provider.md).
For the broader extension model — contracts, manifests, registry,
and conformance harnesses — see
[`docs/extension/overview.md`](docs/extension/overview.md).

> RAGAnything and Graphify are reference provider implementations —
> they are not core identity. The same rules apply to any provider
> you add. Adapters that target the uniform extension surface
> (`j1.extension.contracts`) MUST come with conformance tests
> (see [`docs/extension/conformance-tests.md`](docs/extension/conformance-tests.md)).

---

## 4. Transport isolation

External communication surfaces (REST, webhook, MCP, future
brokers) live in `src/j1/adapters/<name>/`. They MUST:

- Map transport-specific requests → existing port calls on
  `ApplicationFacade`.
- Use `SecurityContext` from the inbound auth layer; never invent a
  parallel one.
- Use the shared `ApplicationEvent` model and the shared
  `EventPublisher` Protocol for any events emitted.
- Honour the no-raise contract on any publication / delivery call
  (publishers must NOT raise; webhook deliveries return failures
  rather than propagate).
- Never import inward of `j1.integration.*`.

The integration layer must never import a transport adapter (no
FastAPI in `j1.integration`, no `httpx` in `j1.integration`, etc.).

---

## 5. Vendor isolation

| Allowed | Forbidden |
|---|---|
| `import openai` inside `src/j1/llm/openai_compat.py` | `import openai` anywhere else |
| `import langchain_core` inside `src/j1/llm/langchain_adapter.py` | `import langchain_core` in `src/j1/processing/*` |
| `import raganything` inside `src/j1/providers/raganything/_bridge.py` | `import raganything` anywhere else |
| `import httpx` inside `src/j1/adapters/webhook/client.py` | `import httpx` in `src/j1/integration/*` or core |

When in doubt: vendor SDKs go behind providers; the core stays
vendor-free.

---

## 6. Configuration and secrets rules

- **All env vars use the `J1_` prefix.** Add them to
  [`.env.example`](.env.example) (with a descriptive comment, no
  real secrets) AND to
  [`docs/configuration/environment.md`](docs/configuration/environment.md).
- **Settings are loaded by typed loaders** (`load_<name>_settings(env=...)`).
  Provider classes take settings objects, never raw env mappings.
- **Secrets never live in committed files.** Use `_FILE` variants
  for secret-manager mounts.
- **Provider-specific config does not pollute core services** —
  `ProcessingService`, the workflow code, and the activity classes
  must remain provider-agnostic.

---

## 7. Testing expectations

- **Hermetic tests.** Use `tmp_path` for filesystem isolation. No
  external services, no network, no live Temporal, no real LLMs.
- **Run before opening a PR:** `.venv/bin/pytest`.
- **Add tests with new behaviour.** A bug fix without a regression
  test is incomplete.
- **For new providers** — three test patterns are required:
  - Injected-callable success / exception normalisation
  - Negative default-path (vendor missing → `ProviderUnavailable`
    with pip-install hint)
  - Positive boundary (mock at the *vendor* seam, not the adapter
    callable, and verify the real boundary was reached)

  Reference: [`tests/test_providers.py`](tests/test_providers.py).
- **Reuse fixtures.** [`tests/conftest.py`](tests/conftest.py) +
  `make_test_environment(tmp_path)` provide wired `WorkspaceResolver`,
  `ProjectContext`, registries, recorders, services, activity
  classes. Don't re-wire from scratch.
- **Cross-layer consistency tests** auto-detect drift across REST +
  integration + events + bulk + AsyncAPI. Don't disable them; if
  they fail, fix the underlying drift.

---

## 8. Documentation expectations

If your change touches behaviour visible to operators or developers:

- **New env var?** Update [`.env.example`](.env.example) AND
  [`docs/configuration/environment.md`](docs/configuration/environment.md).
- **New REST endpoint?** Update [`docs/rest-api.md`](docs/rest-api.md)
  AND ensure the error-codes table in
  [`docs/external-integration-architecture.md`](docs/external-integration-architecture.md)
  is still accurate.
- **New event type?** Update both
  [`docs/event-integration.md`](docs/event-integration.md)
  AND [`docs/asyncapi/kb-events.asyncapi.yaml`](docs/asyncapi/kb-events.asyncapi.yaml)
  — the consistency test fails until both happen.
- **New webhook header / signature behaviour?** Update
  [`docs/webhooks.md`](docs/webhooks.md).
- **New provider?** Update
  [`docs/providers.md`](docs/providers.md) and follow the recipe in
  [`docs/extension/add-a-provider.md`](docs/extension/add-a-provider.md).
- **Architectural change?** Update the relevant section of
  [`docs/architecture.md`](docs/architecture.md).
- **Operational change?** Update
  [`docs/operations/temporal.md`](docs/operations/temporal.md) if
  Temporal-related, or
  [`docs/troubleshooting.md`](docs/troubleshooting.md) if it's a
  failure-mode you've now diagnosed.

Avoid pinning exact test counts in long-lived docs — the suite
grows; the count rots.

---

## 9. PR checklist

Before requesting review:

- [ ] Read the relevant docs and code; understand the seam you're
      modifying.
- [ ] Single-concern change. No drive-by refactors of unrelated code.
- [ ] No domain vocabulary in `src/j1/` (outside `src/j1/profiles/`).
- [ ] No vendor SDK imports outside the allowed locations
      (`src/j1/llm/<vendor>.py`, `src/j1/providers/<vendor>/`,
      `src/j1/adapters/<vendor>/`).
- [ ] No new env vars without documentation in BOTH
      [`.env.example`](.env.example) AND
      [`docs/configuration/environment.md`](docs/configuration/environment.md).
- [ ] All public symbols you added are exported in
      [`src/j1/__init__.py`](src/j1/__init__.py) iff they're meant
      to be public.
- [ ] Tests pass: `.venv/bin/pytest`.
- [ ] New behaviour has at least one test that fails without your
      change.
- [ ] No `# TODO` markers without a referenced issue or a clear
      rationale.
- [ ] No `print()` debugging left behind.
- [ ] Commit messages describe intent ("why"), not just diff
      surface ("what").

---

## 10. Reporting issues

When opening an issue, include:

- The J1 version / commit you're on.
- Minimal reproduction (code snippet or curl invocation).
- Observed vs expected behaviour.
- Relevant logs (with secrets redacted).
- Whether the issue is in core, an adapter, or a provider — your
  best guess is fine; we'll re-route if needed.

---

## 11. Cross-references

- [`docs/development/onboarding.md`](docs/development/onboarding.md)
  — first-time setup
- [`docs/architecture.md`](docs/architecture.md) — what the framework
  is and how it's shaped
- [`docs/external-integration-architecture.md`](docs/external-integration-architecture.md)
  — layer rules + transport boundary
- [`docs/extension/add-a-provider.md`](docs/extension/add-a-provider.md)
  — provider recipe
- [`docs/extension/domain-module-isolation.md`](docs/extension/domain-module-isolation.md)
  — what belongs outside core
- [`docs/configuration/environment.md`](docs/configuration/environment.md)
  — every `J1_*` env var
