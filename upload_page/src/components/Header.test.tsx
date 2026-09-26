import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { Header } from "./Header";

// vitest.config.ts does not set `test.globals: true`, so Testing
// Library's automatic per-test cleanup (which detects a global
// `afterEach`) never registers itself - each component test file wires
// this up explicitly instead of relying on a shared setup file.
afterEach(cleanup);

// The exact banner wording is part of the contract (task brief) - a
// screen reader user relies on this text alone, since the amber color is
// not itself a signal (color-blind users, printouts, etc).
const TEST_MODE_TEXT =
  "TEST MODE \u2014 uploads go to test_collection and expire in about 30 days";

describe("Header", () => {
  it("shows the project and collection", () => {
    render(<Header project="astoriaphotos" collection="lcps_astoria" live />);
    expect(screen.getByText("astoriaphotos")).toBeInTheDocument();
    expect(screen.getByText("lcps_astoria")).toBeInTheDocument();
  });

  it("shows the test-mode banner with the exact wording when not live", () => {
    render(<Header project="astoriaphotos" collection="test_collection" live={false} />);
    expect(screen.getByText(TEST_MODE_TEXT)).toBeInTheDocument();
  });

  it("shows no banner at all in live mode", () => {
    render(<Header project="astoriaphotos" collection="lcps_astoria" live />);
    expect(screen.queryByText(/TEST MODE/)).not.toBeInTheDocument();
  });
});
