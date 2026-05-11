# J1 documentation

This directory is the authoritative documentation for J1. Start
here when you need to understand how the system works, how to
extend it, or how to wire a production worker.

## Start here

| If you want to … | Read |
|---|---|
| Understand the ingestion pipeline | [architecture/ingestion-pipeline.md](architecture/ingestion-pipeline.md) |
| Understand how the system reports a run's outcome | [architecture/final-ingestion-report.md](architecture/final-ingestion-report.md) |
| Understand the post-compile enrichment overlay | [architecture/enrichment-overlay.md](architecture/enrichment-overlay.md) |
| Understand domain customisation (DomainPack) | [architecture/domain-profiles.md](architecture/domain-profiles.md) |
| Get a high-level framework tour | [architecture.md](architecture.md) |

## Architecture (authoritative)

The four files under `architecture/` are the source of truth for
the ingestion pipeline. Together they describe how an upload turns
into a final report.

- [Ingestion pipeline](architecture/ingestion-pipeline.md) — five-stage walkthrough (assessment → compile → post-compile analysis → enrichment → finalization)
- [Domain profiles](architecture/domain-profiles.md) — `DomainPack` + `DomainPromptPack` + `DomainEnrichmentPolicy` + `DomainExtractionHints` + `DomainValidationRules`
- [Enrichment overlay](architecture/enrichment-overlay.md) — `EnrichmentModule` protocol, `CompositeEnrichmentRunner`, `LLMCallLimiter`, prompt-resolution precedence, skip/failure matrix
- [Final ingestion report](architecture/final-ingestion-report.md) — final-status vocabulary, endpoint contract, FE consumption

## Guides (extension recipes)

- [Adding a domain profile](guides/adding-a-domain-profile.md) — register a new `DomainPack` with prompts, hints, and policy
- [Adding an enrichment module](guides/adding-an-enrichment-module.md) — implement an `EnrichmentModule` with typed output projection

## Operations

- [Production worker wiring](operations/production-worker-wiring.md) — deployment checklist + env-var reference
- [Temporal worker setup](operations/temporal.md) — workflow runtime, signals, search attributes
- [Run lifecycle / ingestion operations](ingestion-operations.md) — resume / rebuild-index / full-reindex / delete / batch upload mechanics

## Reference

- [Artifact reference](reference/artifacts.md) — every persisted artifact kind with producer + consumer + endpoint
- [UI / operator copy guide](reference/ui-copy.md) — preferred wording + retired vocabulary
- [REST API](rest-api.md) — endpoint reference
- [Providers](providers.md) — LLM-role registry + provider integrations
- [External integration surface map](external-integration-architecture.md)
- [Configuration / environment variables](configuration/environment.md)

## Frontend

- [Streaming progress + SSE](ingestion-progress.md) — events, macro stages, FE polling-vs-streaming
- [UI copy guide](reference/ui-copy.md)

## Integration surfaces

- [REST API](rest-api.md)
- [Webhooks (HMAC + CloudEvents)](webhooks.md)
- [Event integration / AsyncAPI](event-integration.md)
- [Bulk import / export](bulk.md)
- [MCP status](mcp-status.md)
- [Security primitives](security.md)
- [Integration guide](integration-guide.md)

## Development

- [Onboarding](development/onboarding.md)
- [Troubleshooting](troubleshooting.md)
- [Extension model](extension/overview.md) — the 5-layer model + 12 contracts
- [Contracts reference](extension/contracts.md)
- [Manifest + registry](extension/manifest-and-registry.md)
- [Add a provider](extension/add-a-provider.md)
- [Conformance tests](extension/conformance-tests.md)
- [Domain module isolation rules](extension/domain-module-isolation.md)

## Migration + technical debt

- [Deprecated docs tracker](migration/deprecated-docs.md) — mapping from removed/legacy docs to current replacements
- [Technical debt](tech-debt.md) — known asymmetries + deferred work
