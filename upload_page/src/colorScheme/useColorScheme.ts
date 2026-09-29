import { useCallback, useLayoutEffect, useState, useSyncExternalStore } from "react";
import {
  SYSTEM_DARK_QUERY,
  applyColorScheme,
  readStoredPreference,
  resolveColorScheme,
  safeLocalStorage,
  storePreference,
  type ColorSchemePreference,
} from "./colorScheme";

function subscribeToSystemScheme(onChange: () => void): () => void {
  const query = window.matchMedia(SYSTEM_DARK_QUERY);
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function systemPrefersDark(): boolean {
  return window.matchMedia(SYSTEM_DARK_QUERY).matches;
}

/** The saved preference, the scheme it resolves to, and a setter that saves. */
export function useColorScheme() {
  const [preference, setPreferenceState] = useState(() => readStoredPreference(safeLocalStorage()));
  const prefersDark = useSyncExternalStore(subscribeToSystemScheme, systemPrefersDark);
  const scheme = resolveColorScheme(preference, prefersDark);

  useLayoutEffect(() => {
    applyColorScheme(document.documentElement, scheme);
  }, [scheme]);

  const setPreference = useCallback((next: ColorSchemePreference) => {
    storePreference(safeLocalStorage(), next);
    setPreferenceState(next);
  }, []);

  return { preference, scheme, setPreference };
}
