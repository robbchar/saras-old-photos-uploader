import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { AppVersion } from "./AppVersion";

afterEach(cleanup);

describe("AppVersion", () => {
  it("shows the version with a v prefix", () => {
    render(<AppVersion version="1.2.3" />);
    expect(screen.getByText("v1.2.3")).toBeInTheDocument();
  });

  it("names what the number is for anyone hovering it", () => {
    render(<AppVersion version="1.2.3" />);
    expect(screen.getByText("v1.2.3")).toHaveAttribute("title", "Upload tool version 1.2.3");
  });
});
