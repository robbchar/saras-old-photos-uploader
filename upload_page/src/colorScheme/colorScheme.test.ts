import { afterEach, describe, expect, it } from "vitest";
import {
  COLOR_SCHEME_STORAGE_KEY,
  applyColorScheme,
  isColorSchemePreference,
  readStoredPreference,
  resolveColorScheme,
  storePreference,
} from "./colorScheme";

afterEach(() => localStorage.clear());

describe("isColorSchemePreference", () => {
  it.each(["system", "light", "dark"])("accepts %s", (value) => {
    expect(isColorSchemePreference(value)).toBe(true);
  });

  it.each(["", "sepia", null])("rejects %s", (value) => {
    expect(isColorSchemePreference(value)).toBe(false);
  });
});

describe("readStoredPreference", () => {
  it.each(["light", "dark"] as const)("returns a stored %s choice", (choice) => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, choice);
    expect(readStoredPreference(localStorage)).toBe(choice);
  });

  it("returns system when nothing is stored", () => {
    expect(readStoredPreference(localStorage)).toBe("system");
  });

  it("returns system for an unrecognized stored value", () => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, "sepia");
    expect(readStoredPreference(localStorage)).toBe("system");
  });

  it("returns system when storage is unavailable", () => {
    expect(readStoredPreference(undefined)).toBe("system");
  });

  it("returns system when reading storage throws", () => {
    const throwingStorage = {
      getItem: () => {
        throw new Error("storage disabled");
      },
    } as unknown as Storage;
    expect(readStoredPreference(throwingStorage)).toBe("system");
  });
});

describe("storePreference", () => {
  it.each(["light", "dark"] as const)("saves an explicit %s choice", (choice) => {
    storePreference(localStorage, choice);
    expect(localStorage.getItem(COLOR_SCHEME_STORAGE_KEY)).toBe(choice);
  });

  it("forgets the saved choice when set back to system", () => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, "dark");
    storePreference(localStorage, "system");
    expect(localStorage.getItem(COLOR_SCHEME_STORAGE_KEY)).toBeNull();
  });

  it("does not throw when writing storage throws", () => {
    const throwingStorage = {
      setItem: () => {
        throw new Error("quota");
      },
    } as unknown as Storage;
    expect(() => storePreference(throwingStorage, "dark")).not.toThrow();
  });
});

describe("resolveColorScheme", () => {
  it("follows the system when the preference is system", () => {
    expect(resolveColorScheme("system", true)).toBe("dark");
    expect(resolveColorScheme("system", false)).toBe("light");
  });

  it("ignores the system when the preference is explicit", () => {
    expect(resolveColorScheme("light", true)).toBe("light");
    expect(resolveColorScheme("dark", false)).toBe("dark");
  });
});

describe("applyColorScheme", () => {
  it("sets data-color-scheme on the element", () => {
    const element = document.createElement("html");
    applyColorScheme(element, "dark");
    expect(element.dataset.colorScheme).toBe("dark");
  });
});
