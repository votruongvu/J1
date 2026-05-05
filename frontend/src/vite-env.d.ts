/// <reference types="vite/client" />

/**
 * Compile-time environment variables. Vite inlines `import.meta.env.*`
 * at build time, so anything we surface here MUST be safe to ship
 * publicly in the bundled JS — never put secrets here.
 */

interface ImportMetaEnv {
  /** Default REST base URL the SPA points at on first load. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
