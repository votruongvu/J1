# J1 Configuration Audit V2 — 2026-05-12

Follow-up to `CONFIG_AUDIT.md` (file-level, 2026-05-11). This pass
traces ACTUAL RUNTIME IMPACT for every setting. A field with a read
site is not enough — the value must reach a code path that branches
on it.

## Method

For each Settings dataclass under `src/j1/`:
1. Enumerate every field.
2. Count consumers via grep `<settings_var>.<field_name>` outside
   the settings module itself.
3. For env vars (no dataclass), grep for read sites in `src/j1/` +
   `deploy/`.
4. Verify each "consumer" actually branches on the value, not just
   logs / passes through.

## Confirmed dead (delete in this PR)

### Planning Settings — most fields are write-only orphans

`src/j1/processing/planning_settings.py` defines 16 fields. Outside
the module itself, only 4 are read:

| Consumed (KEEP) | Read site |
| --- | --- |
| `llm_planning_enabled` | `ingestion_review/service.py:1280` |
| `model_profile` | `ingestion_review/service.py:1283,1288,1296` |
| `max_sample_blocks` | `ingestion_review/service.py:1232` |
| `max_preview_chars` | `ingestion_review/service.py:1233` |

| Orphaned (DELETE) | Env var | Note |
| --- | --- | --- |
| `enabled` | `J1_PLANNING_ENABLED` | 0 consumers |
| `post_compile_enabled` | `J1_POST_COMPILE_PLANNING_ENABLED` | 0 consumers |
| `max_early_pages` | `J1_PLANNING_MAX_EARLY_PAGES` | 0 consumers |
| `fail_open` | `J1_PLANNING_FAIL_OPEN` | 0 consumers |
| `trace_enabled` | `J1_PLANNING_TRACE_ENABLED` | 0 consumers |
| `trace_body` | `J1_PLANNING_TRACE_BODY` | 0 consumers |
| `plan_mode` | `J1_INGEST_PLAN_MODE` | env IS used at load to derive `llm_planning_enabled`; field is dead. Keep loader logic, drop the dataclass field. |
| `domain_packs_enabled` | `J1_DOMAIN_PACKS_ENABLED` | 0 consumers from settings (workflow request has its own field, set elsewhere) |
| `default_domain` | `J1_DEFAULT_DOMAIN` | same |
| `domain_detection_enabled` | `J1_DOMAIN_DETECTION_ENABLED` | same |
| `domain_detection_min_confidence` | `J1_DOMAIN_DETECTION_MIN_CONFIDENCE` | same |
| `allowed_domain_overrides` | `J1_ALLOWED_DOMAIN_OVERRIDES` | same |
| `workspace_default_domain` | `J1_WORKSPACE_DEFAULT_DOMAIN` | same |

The domain feature itself stays alive: `ProjectProcessingRequest`
carries `workspace_default_domain`/`allowed_domain_overrides` and the
activities read them. What's dead is the env-var → settings →
request wiring (the dev API entrypoint doesn't thread these through).
Operators currently get no effect from setting these env vars.

### Security Settings — half the fields are stubs

`src/j1/integration/security/settings.py` defines 5 fields. Only
`api_keys` is read by `_wiring.py:357`.

| Field | Env var | Verdict |
| --- | --- | --- |
| `api_keys` | `J1_AUTH_API_KEYS` / `_FILE` | KEEP (only field consumed) |
| `auth_required` | `J1_AUTH_REQUIRED` | DELETE — 0 consumers |
| `anonymous_paths` | `J1_AUTH_ANONYMOUS_PATHS` | DELETE field — 0 consumers from this Settings type. `app.py` has its own `anonymous_paths` parameter fed from a different code path. |
| `jwt_enabled` | `J1_AUTH_JWT_ENABLED` | DELETE — stub for unimplemented JWT support |
| `default_tenant_id` | `J1_AUTH_DEFAULT_TENANT_ID` | DELETE — 0 consumers |

### Standalone env vars with no read site

| Env var | Evidence |
| --- | --- |
| `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` | Referenced ONLY in comments at `project_processing.py:173,2789`. No `os.environ.get(...)` or `source.get(...)` reads it. The workflow flag is set only via direct test construction. Operators setting `=true` get no behavior change. |
| `J1_INGEST_DEFAULT_POLICY` | 0 references in `src/j1/` or `deploy/*.py` or `docker-compose.yml`. Only mentioned in `.env.example`, `.env`, and `docs/configuration/environment.md`. Pure documentation entry. |

### Enrichment Settings — orphan field

| Field | Note |
| --- | --- |
| `dev_mode_conservative_limits` | The env value is consumed at LOAD time to cap other fields (`enrichment_settings.py:224-228`). The stored dataclass field is never read again. Drop the field, keep the loader-time capping logic. |

### Event Publisher loader — not used in production

`src/j1/integration/events/publisher_settings.py` defines a Settings
type loaded by `load_event_publisher_settings`. The function has
zero callers in production (`src/j1/` or `deploy/`). Only tests use
it. The dev stack constructs `EventPublisherService(audit_recorder)`
directly.

`J1_EVENT_PUBLISHER_TYPE`, `J1_EVENT_PUBLISHER_PRODUCER`,
`J1_EVENT_PUBLISHER_SCHEMA_VERSION`, `J1_EVENT_INCLUDE_SENSITIVE_PAYLOADS`
have no production effect. Action: remove from `.env.example`. Keep
the classes (they're public API exported from the `j1` package
namespace) but document the env vars as not wired by the bundled
deploy.

## Confirmed deprecated (already noted, no action this PR)

| Env var | Status |
| --- | --- |
| `J1_LLM_PLANNING_ENABLED` | Already documented as a deprecation alias for `J1_INGEST_PLAN_MODE` in `.env.example`. Keep one more release; can be removed after current users migrate. |

## Confirmed live (no action)

Settings consumed by production code with branching on the value:

- LLM roles (text / vision / embedding / fast) — all fields live.
- RAGAnything provider — all fields live, including
  `supports_image/_table/_equation` (read by plan_mapper).
- Enrichment module ensemble (`enrichment_settings.py`) — all fields
  live except `dev_mode_conservative_limits` (see above).
- Compile retry — all fields live.
- Enrich Assessment fast-LLM consult — all fields live.
- Webhooks — all fields live.
- Temporal connection — all fields live.
- Graphify — all fields live.
- Data root — live.

## Total surface reduction (this PR)

| Layer | Removed |
| --- | --- |
| `PlanningSettings` fields | 13 → 4 (drop 9 fields + 8 env vars; keep `J1_INGEST_PLAN_MODE` env as input to `llm_planning_enabled`) |
| `SecurityAuthenticationSettings` fields | 5 → 1 (drop 4 fields + 4 env vars) |
| `EnrichmentSettings` orphan field | drop 1 |
| Loose env vars in `.env.example` | drop 2 (`J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED`, `J1_INGEST_DEFAULT_POLICY`) |
| Event publisher env vars in `.env.example` | drop 4 |

Net: **27 env vars / dataclass fields removed** from the J1 config
surface in this pass.

## Behavioural guarantees

Every deletion is verified to have 0 runtime effect:

1. Tests that test the deleted fields will be updated to match the
   new dataclass shape — these tests pin behavior that doesn't exist.
2. No deletion changes the code path for any value an operator could
   plausibly have set. The fields are write-only.
3. Public API exports (`j1.EventPublisherSettings`, etc.) remain
   importable for downstream deployments.
4. The `J1_LLM_PLANNING_ENABLED` legacy alias is preserved.

## Out of scope (separate PR)

- Wiring `J1_TEMPORAL_SEARCH_ATTRIBUTES_ENABLED` from env to
  `ProjectProcessingRequest.search_attributes_enabled`.
- Wiring the domain-pack settings from env to
  `ProjectProcessingRequest`.
- Deleting `J1_LLM_PLANNING_ENABLED` after the deprecation window.
- Compile-mode vocabulary cleanup (`fast_text_compile`,
  `multimodal_compile`, `ocr_parse` — needs deeper trace).
