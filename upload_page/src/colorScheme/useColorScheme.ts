import { useCallback, useEffect, useLayoutEffect, useState, useSyncExternalStore } from "react";
import {
  COLOR_SCHEME_STORAGE_KEY,
  SYSTEM_DARK_QUERY,
  applyColorScheme,
  readStoredPreference,
  resolveColorScheme,
  safeLocalStorage,
  storePreference,
  type ColorSchemePreference,
} from "./colorScheme";

let systemDarkQuery: MediaQueryList | undefined;

function systemDarkQueryList(): MediaQueryList {
  systemDarkQuery ??= window.matchMedia(SYSTEM_DARK_QUERY);
  return systemDarkQuery;
}

function subscribeToSystemScheme(onChange: () => void): () => void {
  const query = systemDarkQueryList();
  query.addEventListener("change", onChange);
  return () => query.removeEventListener("change", onChange);
}

function systemPrefersDark(): boolean {
  return systemDarkQueryList().matches;
}

/** The saved preference, the scheme it resolves to, and a setter that saves. */
export function useColorScheme() {
  const [preference, setPreferenceState] = useState(() => readStoredPreference(safeLocalStorage()));
  const prefersDark = useSyncExternalStore(subscribeToSystemScheme, systemPrefersDark);
  const scheme = resolveColorScheme(preference, prefersDark);

  useLayoutEffect(() => {
    applyColorScheme(document.documentElement, scheme);
  }, [scheme]);

  // Another tab's choice; a null key means that tab cleared storage.
  useEffect(() => {
    function handleStorage(event: StorageEvent) {
      if (event.key === COLOR_SCHEME_STORAGE_KEY || event.key === null) {
        setPreferenceState(readStoredPreference(safeLocalStorage()));
      }
    }
    window.addEventListener("storage", handleStorage);
    return () => window.removeEventListener("storage", handleStorage);
  }, []);

  const setPreference = useCallback((next: ColorSchemePreference) => {
    storePreference(safeLocalStorage(), next);
    setPreferenceState(next);
  }, []);

  return { preference, scheme, setPreference };
}
