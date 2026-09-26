import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { Ending, Summary } from "../api/schemas";
import { Finished } from "./Finished";

afterEach(cleanup);

const BASE_SUMMARY: Summary = {
  attempted: 10,
  succeeded: 8,
  failures: [],
  unconfirmed: [],
  not_attempted: 0,
  rate_limited: false,
  rate_limit_status: null,
  stopped_by_request: false,
  skipped: [],
};

describe("Finished", () => {
  it("shows the completed headline with succeeded/failed counts", () => {
    const ending: Ending = {
      kind: "completed",
      summary: { ...BASE_SUMMARY, succeeded: 8, failures: [{ identifier: "lcps-photosexample-00001", error: "HTTP 500" }] },
    };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(screen.getByText("8 uploaded, 1 failed")).toBeInTheDocument();
  });

  it("shows the stopped headline naming how many of the planned total finished", () => {
    const ending: Ending = {
      kind: "stopped",
      planned: 42,
      summary: { ...BASE_SUMMARY, succeeded: 5 },
    };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(screen.getByText("Stopped after 5 of 42")).toBeInTheDocument();
  });

  it("falls back to 'planned' in the stopped headline when the total was never learned", () => {
    const ending: Ending = {
      kind: "stopped",
      planned: null,
      summary: { ...BASE_SUMMARY, succeeded: 5 },
    };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(screen.getByText("Stopped after 5 of planned")).toBeInTheDocument();
  });

  it("shows the exact rate-limited message", () => {
    const ending: Ending = { kind: "rate_limited", summary: BASE_SUMMARY };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(
      screen.getByText("Internet Archive asked us to slow down — try again later"),
    ).toBeInTheDocument();
  });

  it("uses the real em dash (U+2014) in the rate-limited message, not a hyphen or double-hyphen", () => {
    const ending: Ending = { kind: "rate_limited", summary: BASE_SUMMARY };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    const message = screen.getByText(/Internet Archive asked us to slow down/);
    expect(message.textContent).toContain("—");
  });

  it("renders the refusal's reason lines", () => {
    const ending: Ending = {
      kind: "refused",
      reason_lines: ["Sheet is locked by another run.", "Ask an admin to clear the lock."],
    };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(screen.getByText("Sheet is locked by another run.")).toBeInTheDocument();
    expect(screen.getByText("Ask an admin to clear the lock.")).toBeInTheDocument();
  });

  it("shows the ended-without-summary message", () => {
    const ending: Ending = { kind: "ended_without_summary" };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(
      screen.getByText("ended without a summary; run the same theme again to pick up where it stopped"),
    ).toBeInTheDocument();
  });

  it("lists a failure's identifier and error text", () => {
    const ending: Ending = {
      kind: "completed",
      summary: {
        ...BASE_SUMMARY,
        failures: [{ identifier: "lcps-photosexample-00007", error: "HTTP 500 Internal Server Error" }],
      },
    };
    render(<Finished ending={ending} onChooseAnother={vi.fn()} />);
    expect(screen.getByText("lcps-photosexample-00007")).toBeInTheDocument();
    expect(screen.getByText("HTTP 500 Internal Server Error")).toBeInTheDocument();
  });

  it("calls onChooseAnother when 'Choose another theme' is clicked", () => {
    const onChooseAnother = vi.fn();
    const ending: Ending = { kind: "ended_without_summary" };
    render(<Finished ending={ending} onChooseAnother={onChooseAnother} />);
    fireEvent.click(screen.getByRole("button", { name: "Choose another theme" }));
    expect(onChooseAnother).toHaveBeenCalledTimes(1);
  });
});
