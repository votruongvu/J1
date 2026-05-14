# Contributing to J1

Welcome. J1 is a multi-tenant document-centric knowledge platform.
This file captures the rules a contribution must satisfy before it
can land.

If you're new to the codebase, start with
[`docs/05-developer-onboarding.md`](docs/05-developer-onboarding.md)
— it walks you from install to a first ingestion + query. Then
skim [`docs/01-overall-architecture.md`](docs/01-overall-architecture.md)
and the doc that matches what you're changing
([`02-ingestion-flow.md`](docs/02-ingestion-flow.md),
[`03-query-flow.md`](docs/03-query-flow.md),
[`04-core-data-model.md`](docs/04-core-data-model.md)…).

---

## 1. Architecture boundaries

The framework is organised in concentric layers; the dependency
arrow points strictly outward → inward (outer layers depend on
inner, never the reverse):

```
adapters (REST, webhook, …) ← outer, transport-specific
 ↓
integration (ports, DTOs, facade, security, events)
 ↓
core (intake, documents, processing, query, search, artifacts,
      audit, orchestration, validation, providers, domains)
```

The adapter layer must not import from core directly — it talks to
the integration layer's `ApplicationFacade`. The integration layer
must not import from any adapter.

For deeper context see [`docs/01-overall-architecture.md`](docs/01-overall-architecture.md)
and [`docs/09-external-integration-model.md`](docs/09-external-integration-model.md).

---

## 2. Domain-neutral core rule

The core must remain reusable across any document-intelligence
workload. Therefore:

- **No industry vocabulary** anywhere in `src/j1/` outside
  `src/j1/domains/`. No civil / legal / medical / clinical /
  financial / training / customer-name strings, identifiers, or
  comments in core modules.
- **No phase-based / wave-based identifiers** used as core
  concepts. Names like `phase4_runner` or `wave2_indexer` belong
  in changelog notes, not module names.
- **No customer-tenant assumptions** — the default tenant is
  configurable; never hardcoded.

Domain-specific work belongs in a domain pack. See
[`docs/10-domain-configuration.md`](docs/10-domain-configuration.md)
for the schema and the worked Civil Engineering example.

---

## 3. Provider / adapter rules

Vendor SDKs and external tools live behind providers. They must:

- **Be lazy-imported.** Top-level `import vendor_sdk` is forbidden
  in any module that imports cleanly without the optional
  dependency.
- **Be isolated to `src/j1/providers/<name>/`, `src/j1/llm/<vendor>.py`,
  or `src/j1/adapters/<name>/`.** Core processing modules must
  not import vendor SDKs.
- **Never leak vendor objects past their boundary.** Provider
  functions return canonical J1 types (`ArtifactDraft`,
  `ArtifactProcessingResult`, `QueryResult`, `(text, TokenUsage)`,
  etc.). Vendor classes stay inside the provider module.
- **Translate exceptions at the boundary.**
  `ProviderUnavailable` for actionable infra failures;
  `ArtifactProcessingResult(status=FAILED)` for per-call failures.
- **Provide a test seam.** Constructors take an injectable
  callable; tests use it; `from_default(...)` wires the production
  bridge.

RAGAnything is the central compile black box. The same isolation
rules apply to it: J1 core treats compile as one call with one
shape; nothing outside `src/j1/providers/raganything/` may import
RAGAnything internals.

---

## 4. Transport isolation

External communication surfaces (REST, webhook, future brokers)
live in `src/j1/adapters/<name>/`. They MUST:

- Map transport-specific requests → existing port calls on
  `ApplicationFacade`.
- Use `SecurityContext` from the inbound auth layer; never invent
  a parallel one.
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
| --- | --- |
| `import openai` inside `src/j1/llm/openai_compat.py` | `import openai` anywhere else |
| `import raganything` inside `src/j1/providers/raganything/_bridge.py` | `import raganything` anywhere else |
| `import httpx` inside `src/j1/adapters/webhook/client.py` | `import httpx` in `src/j1/integration/*` or core |

When in doubt: vendor SDKs go behind providers; the core stays
vendor-free.

---

## 6. Configuration and secrets rules

- **All env vars use the `J1_` prefix.** Add them to
  [`.env.example`](.env.example) (with a descriptive comment, no
  real secrets) AND surface them in the
  [`docs/05-developer-onboarding.md`](docs/05-developer-onboarding.md)
  or [`docs/07-deployment-and-scaling.md`](docs/07-deployment-and-scaling.md)
  variable tables.
- **Settings are loaded by typed loaders**
  (`load_<name>_settings(env=...)`). Provider classes take settings
  objects, never raw env mappings.
- **Secrets never live in committed files.** Use `_FILE` variants
  for secret-manager mounts.
- **Provider-specific config does not pollute core services** —
  `ProcessingService`, the workflow code, and the activity classes
  must remain provider-agnostic.

---

## 7. Snapshot-centered invariants

The architecture's load-bearing invariants. Code that breaks them
is considered a bug.

- **`active_snapshot_id` is the only visibility key.** No code
  outside the snapshot service may consult `IngestionRun.run_id`
  to decide what is visible.
- **Snapshots are allocated up-front** by the dispatch layer (REST
  for single-doc flows; the `allocate_target_snapshot` activity
  for bulk-job per-doc loops). Activities must not lazily allocate
  via `get_or_create_for_run`.
- **Promotion is CAS-guarded.** A failed CAS means "do not
  promote, run orphan cleanup". Never overwrite the document's
  active snapshot blindly.
- **Compile is a single black box.** Do not re-introduce
  split-mode parsing.
- **Generated test cases are out of scope.** The validation
  surface is Manual Test Query + imported CSV only.

If you're touching any of these, read
[`docs/02-ingestion-flow.md`](docs/02-ingestion-flow.md),
[`docs/03-query-flow.md`](docs/03-query-flow.md), and
[`docs/04-core-data-model.md`](docs/04-core-data-model.md) first.

---

## 8. Testing expectations

- **Hermetic tests.** Use `tmp_path` for filesystem isolation. No
  external services, no network, no live Temporal, no real LLMs.
- **Run before opening a PR:**
  - Backend: `python -m pytest tests/`.
  - Frontend: `cd frontend && npx tsc -b && npx vitest run`.
- **Add tests with new behaviour.** A bug fix without a regression
  test is incomplete.
- **For new providers**, three test patterns are required:
  - Injected-callable success / exception normalisation.
  - Negative default-path (vendor missing → `ProviderUnavailable`
    with pip-install hint).
  - Positive boundary (mock at the *vendor* seam, not the adapter
    callable, and verify the real boundary was reached).
- **Reuse fixtures.** `tests/conftest.py` provides wired
  `WorkspaceResolver`, `ProjectContext`, registries, services. Don't
  re-wire from scratch.

---

## 9. Documentation expectations

If your change touches behaviour visible to operators or
developers, update the matching doc:

| Touching… | Update |
| --- | --- |
| Ingestion behaviour | [`docs/02-ingestion-flow.md`](docs/02-ingestion-flow.md) |
| Query behaviour | [`docs/03-query-flow.md`](docs/03-query-flow.md) |
| Data model (`DocumentRecord`, `DocumentSnapshot`, etc.) | [`docs/04-core-data-model.md`](docs/04-core-data-model.md) |
| Run / test setup | [`docs/05-developer-onboarding.md`](docs/05-developer-onboarding.md) |
| Known limitations or new technical debt | [`docs/06-risks-and-known-limitations.md`](docs/06-risks-and-known-limitations.md) |
| Production-direction changes (env, deployment) | [`docs/07-deployment-and-scaling.md`](docs/07-deployment-and-scaling.md) |
| Multi-KB / scoping rules | [`docs/08-multi-kb-model.md`](docs/08-multi-kb-model.md) |
| REST or event surface | [`docs/09-external-integration-model.md`](docs/09-external-integration-model.md) |
| Domain packs | [`docs/10-domain-configuration.md`](docs/10-domain-configuration.md) |
| New `J1_*` env var | `.env.example` AND the relevant doc's variable table |

Avoid pinning exact test counts or "phase / wave / patch" labels
in long-lived docs.

---

## 10. PR checklist

Before requesting review:

- [ ] Read the relevant docs and code; understand the seam you're
      modifying.
- [ ] Single-concern change. No drive-by refactors of unrelated
      code.
- [ ] No domain vocabulary in `src/j1/` outside `src/j1/domains/`.
- [ ] No vendor SDK imports outside the allowed locations.
- [ ] No new env vars without documentation in `.env.example` AND
      a doc-set table.
- [ ] All public symbols you added are exported in
      `src/j1/__init__.py` iff they're meant to be public.
- [ ] Backend tests pass: `python -m pytest tests/`.
- [ ] Frontend tests pass: `cd frontend && npx tsc -b && npx vitest run`.
- [ ] New behaviour has at least one test that fails without your
      change.
- [ ] No `# TODO` markers without a referenced issue or a clear
      rationale.
- [ ] No `print` debugging left behind.
- [ ] Commit messages describe intent ("why"), not just diff
      surface ("what").

---

## 11. Reporting issues

When opening an issue, include:

- The J1 version / commit you're on.
- Minimal reproduction (code snippet or curl invocation).
- Observed vs expected behaviour.
- Relevant logs (with secrets redacted).
- Whether the issue is in core, an adapter, or a provider — your
  best guess is fine; we'll re-route if needed.

---

## 12. Cross-references

- [`README.md`](README.md) — project entry point.
- [`docs/01-overall-architecture.md`](docs/01-overall-architecture.md)
  — system overview.
- [`docs/05-developer-onboarding.md`](docs/05-developer-onboarding.md)
  — first-time setup.
- [`docs/04-core-data-model.md`](docs/04-core-data-model.md) —
  the durable nouns.
- [`docs/06-risks-and-known-limitations.md`](docs/06-risks-and-known-limitations.md)
  — what's open + acknowledged.
- [`docs/09-external-integration-model.md`](docs/09-external-integration-model.md)
  — REST + event boundary.
- [`docs/10-domain-configuration.md`](docs/10-domain-configuration.md)
  — adding domain packs.
