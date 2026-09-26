// App owns every fetch, the SSE subscription, and the health-reload
// timer - everything this file needs to verify - so ./api/client is
// mocked wholesale; no real network call happens in any test here.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { OutputHandlers } from "./api/client";
import type { Ending, Health, Status, ValidateDoc, ValidateRow } from "./api/schemas";
import App from "./App";

// vi.mock's factory is hoisted above every other statement in this file,
// so the mocks it returns must themselves come from vi.hoisted - a plain
// `const mockX = vi.fn()` above it would still run *after* the factory.
const { mockGetStatus, mockGetThemes, mockGetPreview, mockGetHealth, mockStartRun, mockStopRun, mockOpenOutput } =
  vi.hoisted(() => ({
    mockGetStatus: vi.fn(),
    mockGetThemes: vi.fn(),
    mockGetPreview: vi.fn(),
    mockGetHealth: vi.fn(),
    mockStartRun: vi.fn(),
    mockStopRun: vi.fn(),
    mockOpenOutput: vi.fn(),
  }));

vi.mock("./api/client", () => ({
  getStatus: mockGetStatus,
  getThemes: mockGetThemes,
  getPreview: mockGetPreview,
  getHealth: mockGetHealth,
  startRun: mockStartRun,
  stopRun: mockStopRun,
  openOutput: mockOpenOutput,
}));

// Mirrors App.tsx's own HEALTH_POLL_INTERVAL_MS - not exported, so the
// health-reload test keeps its own copy rather than reaching into the
// module's internals.
const HEALTH_POLL_INTERVAL_MS = 10_000;

// jsdom has no layout engine, so it never implemented scrollIntoView -
// Radix's Select scrolls the selected/first item into view as soon as its
// listbox opens (see ThemePicker.test.tsx for the same polyfill).
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

const ZERO_VERDICTS = { ready: 0, invalid: 0, not_ready: 0 };
const ZERO_COUNTS = { unassigned: ZERO_VERDICTS, done: ZERO_VERDICTS, reserved: ZERO_VERDICTS };

const STATUS_IDLE: Status = {
  live: false,
  project: "astoriaphotos",
  collection: "sarasoldphotos",
  run: { kind: "idle" },
};

const THEMES: ValidateDoc = {
  format: 1,
  project: "astoriaphotos",
  live: false,
  batch: null,
  valid: true,
  sheet_errors: [],
  rows_with_errors: [],
  ready_to_upload: 5,
  counts: ZERO_COUNTS,
  batches: [
    {
      value: "Fishing",
      ready_to_upload: 5,
      counts: { ...ZERO_COUNTS, unassigned: { ready: 5, invalid: 0, not_ready: 0 } },
    },
  ],
  rows: null,
};

const READY_ROWS: ValidateRow[] = Array.from({ length: 5 }, (_, index) => ({
  row: index + 2,
  state: "unassigned",
  verdict: "ready",
  identifier: `lcps-photosexample-${String(index + 1).padStart(5, "0")}`,
  errors: [],
  missing_fields: [],
}));

const PREVIEW: ValidateDoc = {
  format: 1,
  project: "astoriaphotos",
  live: false,
  batch: "Fishing",
  valid: true,
  sheet_errors: [],
  rows_with_errors: [],
  ready_to_upload: 5,
  counts: ZERO_COUNTS,
  batches: null,
  rows: READY_ROWS,
};

const HEALTH_V1: Health = { commit: "abc123", bundle_stamp: "stamp-1", live: false, project: "astoriaphotos" };
const HEALTH_V2: Health = { ...HEALTH_V1, bundle_stamp: "stamp-2" };

const COMPLETED_ENDING: Ending = {
  kind: "completed",
  summary: {
    attempted: 5,
    succeeded: 5,
    failures: [],
    unconfirmed: [],
    not_attempted: 0,
    rate_limited: false,
    rate_limit_status: null,
    stopped_by_request: false,
    skipped: [],
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  mockGetStatus.mockResolvedValue(STATUS_IDLE);
  mockGetThemes.mockResolvedValue(THEMES);
  mockGetPreview.mockResolvedValue(PREVIEW);
  mockGetHealth.mockResolvedValue(HEALTH_V1);
  mockStartRun.mockResolvedValue({ started_at: "2026-09-25T12:00:00Z" });
  mockStopRun.mockResolvedValue(undefined);
  mockOpenOutput.mockImplementation(() => ({ close: vi.fn() }));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

/** Drives the app from mount through a loaded preview: opens the theme
 * picker, selects "Fishing", and waits for its preview to render. */
async function selectFishingTheme() {
  render(<App />);
  const trigger = await screen.findByRole("combobox", { name: /choose a theme/i });
  fireEvent.click(trigger);
  fireEvent.click(await screen.findByRole("option", { name: /Fishing/ }));
  await screen.findByText("5 ready to upload");
}

describe("App", () => {
  it("shows the theme picker once status is idle and themes have loaded", async () => {
    render(<App />);
    expect(await screen.findByRole("combobox", { name: /choose a theme/i })).toBeInTheDocument();
    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(mockGetThemes).toHaveBeenCalledTimes(1);
  });

  it("selecting a theme fetches and shows its preview", async () => {
    await selectFishingTheme();
    expect(mockGetPreview).toHaveBeenCalledWith("Fishing");
  });

  it("confirming the start dialog starts the run and shows the output pane", async () => {
    await selectFishingTheme();

    fireEvent.click(screen.getByRole("button", { name: "Upload 5 photos to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(mockStartRun).toHaveBeenCalledWith("Fishing"));
    expect(await screen.findByRole("log")).toBeInTheDocument();
    expect(mockOpenOutput).toHaveBeenCalledTimes(1);
  });

  it("shows the Finished screen once the output stream reports the run ended", async () => {
    let capturedHandlers: OutputHandlers | undefined;
    mockOpenOutput.mockImplementation((handlers: OutputHandlers) => {
      capturedHandlers = handlers;
      return { close: vi.fn() };
    });

    await selectFishingTheme();
    fireEvent.click(screen.getByRole("button", { name: "Upload 5 photos to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await screen.findByRole("log");

    act(() => {
      capturedHandlers?.onFinished(COMPLETED_ENDING);
    });

    expect(await screen.findByText("5 uploaded, 0 failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Choose another theme" })).toBeInTheDocument();
  });

  it("reloads the page once the health bundle stamp changes", async () => {
    vi.useFakeTimers();
    mockGetHealth.mockResolvedValueOnce(HEALTH_V1).mockResolvedValue(HEALTH_V2);
    const reload = vi.fn();
    Object.defineProperty(window, "location", {
      value: { ...window.location, reload },
      writable: true,
      configurable: true,
    });

    render(<App />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(HEALTH_POLL_INTERVAL_MS);
    });

    expect(reload).toHaveBeenCalledTimes(1);
  });
});
