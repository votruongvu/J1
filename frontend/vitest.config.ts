/**
 * Vitest config — small, opinionated.
 *
 * Tests live alongside source under `src/**\/__tests__/*.test.ts`. We
 * keep the default `node` environment because every test in this
 * suite stubs the bits of the browser API it needs (fetch / Response /
 * ReadableStream) rather than depending on jsdom; that keeps the
 * runtime small and the assertions explicit. If a future test needs
 * DOM rendering, switch its file's `environment` via the doc-comment
 * pragma `// @vitest-environment jsdom`.
 */

import { defineConfig } from "vitest/config";
import path from "node:path";

export default defineConfig({
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  test: {
    environment: "node",
    globals: false,
    include: ["src/**/__tests__/**/*.test.ts"],
  },
});
