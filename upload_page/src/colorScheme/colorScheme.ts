// Light/dark color scheme rules. "system" follows the OS; an explicit choice
// is saved and wins until set back to "system". index.html repeats the read
// side of this inline so the first paint is already correct - keep them in step.

export type ColorScheme = "light" | "dark";
export type ColorSchemePreference = ColorScheme | "system";

export const COLOR_SCHEME_STORAGE_KEY = "upload-page:color-scheme";
export const SYSTEM_DARK_QUERY = "(prefers-color-scheme: dark)";

function isColorScheme(value: unknown): value is ColorScheme {
  return value === "light" || value === "dark";
}

/** localStorage, or undefined where the browser blocks it. */
export function safeLocalStorage(): Storage | undefined {
  try {
    return window.localStorage;
  } catch {
    return undefined;
  }
}

export function readStoredPreference(storage: Storage | undefined): ColorSchemePreference {
  try {
    const stored = storage?.getItem(COLOR_SCHEME_STORAGE_KEY);
    return isColorScheme(stored) ? stored : "system";
  } catch {
    return "system";
  }
}

export function storePreference(storage: Storage | undefined, preference: ColorSchemePreference): void {
  try {
    if (preference === "system") storage?.removeItem(COLOR_SCHEME_STORAGE_KEY);
    else storage?.setItem(COLOR_SCHEME_STORAGE_KEY, preference);
  } catch {
    // Unsaved is acceptable: the choice still applies for this visit.
  }
}

export function resolveColorScheme(preference: ColorSchemePreference, systemPrefersDark: boolean): ColorScheme {
  if (preference !== "system") return preference;
  return systemPrefersDark ? "dark" : "light";
}

/** index.css keys every color token off this attribute. */
export function applyColorScheme(element: HTMLElement, scheme: ColorScheme): void {
  element.dataset.colorScheme = scheme;
}
