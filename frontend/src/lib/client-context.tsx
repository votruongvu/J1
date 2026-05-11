/**
 * React context that provides the active `IngestionClient`.
 *
 * The prototype attached the client to `window.client`; the migrated
 * codebase routes through context so:
 * 1. components are testable without globals,
 * 2. switching between mock and live mode is a single state change,
 * 3. type-checking flows through `useClient` instead of casting
 * a `window` lookup.
 */

import { createContext, useContext } from "react";
import type { IngestionClient } from "./api/client";

export const ClientContext = createContext<IngestionClient | null>(null);

export function useClient(): IngestionClient {
  const client = useContext(ClientContext);
  if (!client) {
    throw new Error(
      "useClient() called outside <ClientProvider>. Wrap the app in ClientProvider.",
    );
  }
  return client;
}
