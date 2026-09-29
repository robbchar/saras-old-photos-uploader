import { afterEach, describe, expect, it } from "vitest";
import { act, cleanup, renderHook } from "@testing-library/react";
import { COLOR_SCHEME_STORAGE_KEY } from "./colorScheme";
import { useColorScheme } from "./useColorScheme";
import { setSystemPrefersDark } from "../test/matchMedia";

afterEach(() => {
  cleanup();
  localStorage.clear();
  delete document.documentElement.dataset.colorScheme;
});

describe("useColorScheme", () => {
  it("starts on system and follows the OS preference", () => {
    setSystemPrefersDark(true);
    const { result } = renderHook(() => useColorScheme());
    expect(result.current.preference).toBe("system");
    expect(result.current.scheme).toBe("dark");
    expect(document.documentElement.dataset.colorScheme).toBe("dark");
  });

  it("tracks OS changes while on system", () => {
    setSystemPrefersDark(false);
    const { result } = renderHook(() => useColorScheme());
    act(() => setSystemPrefersDark(true));
    expect(result.current.scheme).toBe("dark");
    expect(document.documentElement.dataset.colorScheme).toBe("dark");
  });

  it("starts on a previously saved choice", () => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, "light");
    setSystemPrefersDark(true);
    const { result } = renderHook(() => useColorScheme());
    expect(result.current.preference).toBe("light");
    expect(result.current.scheme).toBe("light");
  });

  it("saves an explicit choice and stops following the OS", () => {
    setSystemPrefersDark(false);
    const { result } = renderHook(() => useColorScheme());
    act(() => result.current.setPreference("dark"));
    expect(localStorage.getItem(COLOR_SCHEME_STORAGE_KEY)).toBe("dark");
    act(() => setSystemPrefersDark(false));
    expect(result.current.scheme).toBe("dark");
    expect(document.documentElement.dataset.colorScheme).toBe("dark");
  });

  it("forgets the saved choice when set back to system", () => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, "dark");
    setSystemPrefersDark(false);
    const { result } = renderHook(() => useColorScheme());
    act(() => result.current.setPreference("system"));
    expect(localStorage.getItem(COLOR_SCHEME_STORAGE_KEY)).toBeNull();
    expect(result.current.scheme).toBe("light");
  });
});
