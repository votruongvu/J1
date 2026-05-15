/**
 * Frontend feature flags.
 *
 * These switch UI behaviour without backend cooperation — every
 * value is read at module load time from ``import.meta.env`` (Vite)
 * so a deployment can flip a flag by setting the matching env var
 * before ``npm run build``.
 *
 * Wire vocabulary mirrors the user-facing showcase spec:
 *
 *   ``manualActionsEnabled`` — when true, the FE renders the
 *     explicit post-index Manual Actions surface (LLM Advanced
 *     Assessment, Domain Enrichment, etc.) and hides legacy
 *     primary actions that overlap with them.
 *   ``hideLegacyRefreshEnrich`` — independent override for the
 *     specific "Refresh Enrichment" Run Detail button. Defaults
 *     to ``true`` whenever ``manualActionsEnabled`` is true; can
 *     be flipped on its own for an emergency rollback.
 */


function _envBool(key: string, fallback: boolean): boolean {
  // ``import.meta.env`` is statically replaced by Vite at build
  // time — never re-evaluates after the bundle ships. That's
  // intentional: a deployment changes flags by re-building, not
  // by serving a different runtime config.
  const raw = (import.meta.env as Record<string, string | undefined>)[key];
  if (raw === undefined || raw === null || raw === "") return fallback;
  const v = String(raw).trim().toLowerCase();
  if (v === "true" || v === "1" || v === "yes" || v === "on") return true;
  if (v === "false" || v === "0" || v === "no" || v === "off") return false;
  return fallback;
}


/** Manual Actions mode. Defaults to ``true`` for the showcase
 * build — the spec wants explicit post-index actions to be the
 * default user-facing model. Operators rolling back can set
 * ``VITE_J1_MANUAL_ACTIONS_ENABLED=false``. */
export const manualActionsEnabled: boolean = _envBool(
  "VITE_J1_MANUAL_ACTIONS_ENABLED", true,
);


/** Hide the legacy "Refresh Enrichment" Run Detail button. When
 * ``manualActionsEnabled`` is true this defaults to ``true``
 * automatically; deployments can flip it independently via
 * ``VITE_J1_HIDE_LEGACY_REFRESH_ENRICH``. */
export const hideLegacyRefreshEnrich: boolean = _envBool(
  "VITE_J1_HIDE_LEGACY_REFRESH_ENRICH",
  manualActionsEnabled,
);
