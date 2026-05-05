/**
 * Persisted string state.
 *
 * Mirrors the prototype's tiny `useLocalStorage` helper but typed and
 * generic over a string union. A `null` or empty value clears the key,
 * which keeps the migration of the prototype's auth-clear flow simple.
 *
 * Storage failures (private mode, quota) are swallowed so the UI stays
 * usable when persistence is denied — the in-memory state still works.
 */

import { useCallback, useState } from "react";

export const LS_KEYS = {
  tenant: "j1.tenantId",
  project: "j1.projectId",
  authKind: "j1.authKind",
  authValue: "j1.authValue",
  apiBase: "j1.apiBase",
  theme: "j1.theme",
  mode: "j1.mode",
  scenario: "j1.scenario",
} as const;

export function useLocalStorage<T extends string = string>(
  key: string,
  initial: NoInfer<T>,
): [T, (next: T) => void] {
  const [value, setValue] = useState<T>(() => {
    try {
      const raw = localStorage.getItem(key);
      return raw == null ? initial : (raw as T);
    } catch {
      return initial;
    }
  });

  const set = useCallback(
    (next: T) => {
      setValue(next);
      try {
        if (next == null || next === "") localStorage.removeItem(key);
        else localStorage.setItem(key, next);
      } catch {
        /* ignore storage errors */
      }
    },
    [key],
  );

  return [value, set];
}
