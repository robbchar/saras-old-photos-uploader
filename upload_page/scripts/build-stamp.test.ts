import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { computeBuildStamp } from "./build-stamp.mjs";

// Pinned in test_build_stamp.py (EXPECTED_FIXTURE_STAMP). Keeping the same
// literal here locks this JS implementation to the Python one for the same
// input tree, without either side ever invoking the other.
const EXPECTED_FIXTURE_STAMP =
  "9ce5f51497020ddc6be31a8afc13411500a3c5da0fdee86f1614b5d4506c01a2";

const FIXTURE_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "build_stamp_fixture"
);

describe("computeBuildStamp", () => {
  it("matches the stamp build_stamp.py computes for the shared fixture tree", () => {
    expect(computeBuildStamp(FIXTURE_DIR)).toBe(EXPECTED_FIXTURE_STAMP);
  });
});
