import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { computeBuildStamp } from "./build-stamp.mjs";

// Pinned in test_build_stamp.py (EXPECTED_FIXTURE_STAMP). Keeping the same
// literal here locks this JS implementation to the Python one for the same
// input tree, without either side ever invoking the other.
const EXPECTED_FIXTURE_STAMP =
  "f981dc8160b2be7ec975e6e008eba37d678c88b69b9545a214e8718613ab8beb";

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
