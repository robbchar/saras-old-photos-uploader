// index.html carries an inline copy of the color-scheme rule so the page
// paints in the right scheme before React loads; these run that exact script.

import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { COLOR_SCHEME_STORAGE_KEY } from "./colorScheme";
import { setSystemPrefersDark } from "../test/matchMedia";

const indexHtml = readFileSync(resolve(process.cwd(), "index.html"), "utf8");
const inlineScript = /<script>([\s\S]*?)<\/script>/.exec(indexHtml)?.[1];

function runBootstrapScript(): void {
  if (!inlineScript) throw new Error("index.html has no inline <script>");
  new Function(inlineScript)();
}

afterEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.colorScheme;
});

describe("index.html color-scheme bootstrap", () => {
  it("uses the OS preference when nothing is saved", () => {
    setSystemPrefersDark(true);
    runBootstrapScript();
    expect(document.documentElement.dataset.colorScheme).toBe("dark");
  });

  it("uses the saved choice over the OS preference", () => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, "light");
    setSystemPrefersDark(true);
    runBootstrapScript();
    expect(document.documentElement.dataset.colorScheme).toBe("light");
  });

  it("falls back to the OS preference for an unrecognized saved value", () => {
    localStorage.setItem(COLOR_SCHEME_STORAGE_KEY, "sepia");
    setSystemPrefersDark(false);
    runBootstrapScript();
    expect(document.documentElement.dataset.colorScheme).toBe("light");
  });
});
