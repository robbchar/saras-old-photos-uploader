import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import type { ValidateDoc, ValidateRow } from "../api/schemas";
import { Preview } from "./Preview";

afterEach(cleanup);

const ZERO_VERDICTS = { ready: 0, invalid: 0, not_ready: 0 };
const ZERO_COUNTS = { unassigned: ZERO_VERDICTS, done: ZERO_VERDICTS, reserved: ZERO_VERDICTS };

function docWithRows(rows: ValidateRow[]): ValidateDoc {
  return {
    format: 1,
    project: "astoriaphotos",
    live: false,
    batch: "Fishing",
    valid: false,
    sheet_errors: [],
    rows_with_errors: [],
    ready_to_upload: rows.filter((row) => row.verdict === "ready").length,
    counts: ZERO_COUNTS,
    batches: null,
    rows,
  };
}

const MISSING_FILE_ERROR = "no file found in '<files_dir>' matching 'photo3.jpg'";

describe("Preview", () => {
  it("shows the ready count", () => {
    const doc = docWithRows([
      { row: 2, state: "unassigned", verdict: "ready", identifier: "", errors: [], missing_fields: [] },
      { row: 7, state: "done", verdict: "ready", identifier: "lcps-astoriaphotos-00001", errors: [], missing_fields: [] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText("2 ready to upload")).toBeInTheDocument();
  });

  it("compresses two contiguous invalid rows sharing the same reason into a range", () => {
    const doc = docWithRows([
      { row: 10, state: "unassigned", verdict: "invalid", identifier: "", errors: [MISSING_FILE_ERROR], missing_fields: [] },
      { row: 11, state: "unassigned", verdict: "invalid", identifier: "", errors: [MISSING_FILE_ERROR], missing_fields: [] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText(`rows 10-11: ${MISSING_FILE_ERROR}`)).toBeInTheDocument();
  });

  it("does not merge invalid rows across a gap in row numbers", () => {
    const doc = docWithRows([
      { row: 10, state: "unassigned", verdict: "invalid", identifier: "", errors: [MISSING_FILE_ERROR], missing_fields: [] },
      { row: 12, state: "unassigned", verdict: "invalid", identifier: "", errors: [MISSING_FILE_ERROR], missing_fields: [] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText(`row 10: ${MISSING_FILE_ERROR}`)).toBeInTheDocument();
    expect(screen.getByText(`row 12: ${MISSING_FILE_ERROR}`)).toBeInTheDocument();
  });

  it("does not merge contiguous invalid rows with different reasons", () => {
    const doc = docWithRows([
      { row: 10, state: "unassigned", verdict: "invalid", identifier: "", errors: ["error A"], missing_fields: [] },
      { row: 11, state: "unassigned", verdict: "invalid", identifier: "", errors: ["error B"], missing_fields: [] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText("row 10: error A")).toBeInTheDocument();
    expect(screen.getByText("row 11: error B")).toBeInTheDocument();
  });

  it("falls back to a plain label when an invalid row carries no errors", () => {
    const doc = docWithRows([
      { row: 15, state: "unassigned", verdict: "invalid", identifier: "", errors: [], missing_fields: [] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText("row 15: invalid")).toBeInTheDocument();
  });

  it("shows a not-ready row with its missing field", () => {
    const doc = docWithRows([
      { row: 20, state: "unassigned", verdict: "not_ready", identifier: "", errors: [], missing_fields: ["title"] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText("row 20: needs title")).toBeInTheDocument();
  });

  it("joins several missing fields for one not-ready row", () => {
    const doc = docWithRows([
      { row: 21, state: "unassigned", verdict: "not_ready", identifier: "", errors: [], missing_fields: ["title", "date"] },
    ]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText("row 21: needs title, date")).toBeInTheDocument();
  });

  it("shows checked-at as HH:MM derived from checkedAt", () => {
    const doc = docWithRows([]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={vi.fn()} />);
    expect(screen.getByText(/checked at 09:07/)).toBeInTheDocument();
  });

  it("fires onRecheck when Re-check is clicked", () => {
    const onRecheck = vi.fn();
    const doc = docWithRows([]);
    render(<Preview doc={doc} checkedAt="2026-01-01T09:07:00" onRecheck={onRecheck} />);
    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    expect(onRecheck).toHaveBeenCalledTimes(1);
  });
});
