import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const indexCss = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "index.css"), "utf8");

function declarations(block: string | undefined): string[] {
  if (block === undefined) throw new Error("index.css block not found");
  return block
    .split(";")
    .map((declaration) => declaration.trim())
    .filter(Boolean);
}

describe("index.css dark tokens", () => {
  it("keeps the no-script prefers-color-scheme fallback identical to the attribute block", () => {
    const attributeBlock = /:root\[data-color-scheme="dark"\]\s*\{([^}]*)\}/.exec(indexCss)?.[1];
    const fallbackBlock = /@media \(prefers-color-scheme: dark\)\s*\{\s*:root:not\(\[data-color-scheme\]\)\s*\{([^}]*)\}/.exec(
      indexCss,
    )?.[1];
    expect(declarations(fallbackBlock)).toEqual(declarations(attributeBlock));
  });
});
