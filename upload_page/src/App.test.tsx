// App owns every fetch, the SSE subscription, and the health-reload
// timer - everything this file needs to verify - so ./api/client is
// mocked wholesale; no real network call happens in any test here.

import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { OutputHandlers } from "./api/client";
import type { Ending, Health, Status, ValidateDoc, ValidateRow } from "./api/schemas";
import App, { HEALTH_POLL_INTERVAL_MS, appendLineCapped } from "./App";

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
  version: "1.0.0",
  run: { kind: "idle" },
};

// A run already active when the page loads - a fresh mount mid-run, a
// second tab, or a reload triggered mid-upload (e.g. by the health-reload
// effect itself) all boot straight into this via getStatus().
const STATUS_RUNNING_MID_UPLOAD: Status = {
  live: false,
  project: "astoriaphotos",
  collection: "sarasoldphotos",
  version: "1.0.0",
  run: {
    kind: "page_run_active",
    batch: "Fishing",
    live: false,
    started_at: "2026-09-25T12:00:00Z",
    done: 2,
    planned: 5,
    current: null,
  },
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
  file: `photo${index + 1}.jpg`,
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
  // The output-offset resume key lives in sessionStorage, which jsdom keeps
  // across tests in the same file - start each test with a clean slate so
  // one test's remembered offset can't leak into the next.
  window.sessionStorage.clear();
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

describe("appendLineCapped", () => {
  it("appends normally while under the cap", () => {
    const result = appendLineCapped(["a", "b"], "c");
    expect(result).toEqual(["a", "b", "c"]);
  });

  it("keeps exactly the newest 2000 lines, dropping the oldest, once over the cap", () => {
    const lines = Array.from({ length: 2000 }, (_, i) => `line-${i}`);
    let buffer: string[] = [];
    for (const line of lines) {
      buffer = appendLineCapped(buffer, line);
    }
    buffer = appendLineCapped(buffer, "line-2000");

    expect(buffer).toHaveLength(2000);
    expect(buffer[0]).toBe("line-1");
    expect(buffer.at(-1)).toBe("line-2000");
  });

  it("stays bounded at 2000 across many more appends than the cap", () => {
    let buffer: string[] = [];
    for (let i = 0; i < 5000; i++) {
      buffer = appendLineCapped(buffer, `line-${i}`);
    }

    expect(buffer).toHaveLength(2000);
    expect(buffer[0]).toBe("line-3000");
    expect(buffer.at(-1)).toBe("line-4999");
  });
});

describe("App", () => {
  it("shows the theme picker once status is idle and themes have loaded", async () => {
    render(<App />);
    expect(await screen.findByRole("combobox", { name: /choose a theme/i })).toBeInTheDocument();
    expect(mockGetStatus).toHaveBeenCalledTimes(1);
    expect(mockGetThemes).toHaveBeenCalledTimes(1);
  });

  it("shows the server-reported app version in the header", async () => {
    render(<App />);
    expect(await screen.findByText("v1.0.0")).toBeInTheDocument();
  });

  it("selecting a theme fetches and shows its preview", async () => {
    await selectFishingTheme();
    expect(mockGetPreview).toHaveBeenCalledWith("Fishing");
  });

  it("updates the dropdown label when a re-check changes a theme's readiness", async () => {
    const notReadyThemes: ValidateDoc = {
      ...THEMES,
      ready_to_upload: 0,
      batches: [
        {
          value: "Fishing",
          ready_to_upload: 0,
          counts: { ...ZERO_COUNTS, unassigned: { ready: 0, invalid: 0, not_ready: 2 } },
        },
      ],
    };
    const twoNotReadyRows: ValidateRow[] = [
      { row: 3, state: "unassigned", verdict: "not_ready", identifier: "", file: "a.jpg", errors: [], missing_fields: ["title"] },
      { row: 4, state: "unassigned", verdict: "not_ready", identifier: "", file: "b.jpg", errors: [], missing_fields: ["title"] },
    ];
    const zeroReadyPreview: ValidateDoc = {
      ...PREVIEW,
      ready_to_upload: 0,
      counts: { ...ZERO_COUNTS, unassigned: { ready: 0, invalid: 0, not_ready: 2 } },
      rows: twoNotReadyRows,
    };
    const oneReadyPreview: ValidateDoc = {
      ...PREVIEW,
      ready_to_upload: 1,
      counts: { ...ZERO_COUNTS, unassigned: { ready: 1, invalid: 0, not_ready: 1 } },
      rows: [{ ...twoNotReadyRows[0], verdict: "ready", missing_fields: [] }, twoNotReadyRows[1]],
    };
    mockGetThemes.mockResolvedValue(notReadyThemes);
    mockGetPreview.mockResolvedValueOnce(zeroReadyPreview).mockResolvedValueOnce(oneReadyPreview);

    render(<App />);
    const trigger = await screen.findByRole("combobox", { name: /choose a theme/i });
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("option", { name: "Fishing — 2 not ready" }));
    await screen.findByText("0 ready to upload");
    expect(trigger).toHaveTextContent("Fishing — 2 not ready");

    fireEvent.click(screen.getByRole("button", { name: "Re-check" }));
    await screen.findByText("1 ready to upload");
    // The dropdown reflects the re-check, not the stale all-mode count.
    expect(trigger).toHaveTextContent("Fishing — 1 ready");
  });

  it("confirming the start dialog starts the run and shows the output pane", async () => {
    await selectFishingTheme();

    fireEvent.click(screen.getByRole("button", { name: "Upload 5 items to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    await waitFor(() => expect(mockStartRun).toHaveBeenCalledWith("Fishing"));
    expect(await screen.findByRole("log")).toBeInTheDocument();
    expect(mockOpenOutput).toHaveBeenCalledTimes(1);
  });

  it("waits for startRun to succeed before opening the output stream (no race against the new run existing)", async () => {
    const callOrder: string[] = [];
    mockStartRun.mockImplementation(async (batch: string) => {
      callOrder.push(`startRun:${batch}`);
      return { started_at: "2026-09-25T12:00:00Z" };
    });
    mockOpenOutput.mockImplementation(() => {
      callOrder.push("openOutput");
      return { close: vi.fn() };
    });

    await selectFishingTheme();
    fireEvent.click(screen.getByRole("button", { name: "Upload 5 items to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    // startRun's promise has not resolved yet at this synchronous point -
    // the stream must not open (and the page must not claim "running")
    // until it does.
    expect(mockOpenOutput).not.toHaveBeenCalled();
    expect(screen.queryByRole("log")).not.toBeInTheDocument();

    await waitFor(() => expect(mockOpenOutput).toHaveBeenCalledTimes(1));
    expect(callOrder).toEqual(["startRun:Fishing", "openOutput"]);
    expect(await screen.findByRole("log")).toBeInTheDocument();
  });

  it("shows no Cancel/Confirm affordance while starting, so a run already created can't be orphaned", async () => {
    let resolveStartRun: (value: { started_at: string }) => void = () => {};
    mockStartRun.mockImplementation(
      () => new Promise((resolve) => { resolveStartRun = resolve; }),
    );

    await selectFishingTheme();
    fireEvent.click(screen.getByRole("button", { name: "Upload 5 items to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    // No Cancel, no re-Confirm, and the theme picker is disabled - nothing
    // can interleave with the in-flight startRun and revert state while the
    // run it started goes on existing server-side.
    expect(await screen.findByText("Starting upload…")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm" })).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /choose a theme/i })).toBeDisabled();

    await act(async () => {
      resolveStartRun({ started_at: "2026-09-25T12:00:00Z" });
      await Promise.resolve();
    });

    // The run that now exists server-side is not orphaned: the page lands
    // on "running", with the picker disabled so no theme switch can start a
    // second run.
    expect(await screen.findByRole("log")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /choose a theme/i })).toBeDisabled();
  });

  it("shows an error instead of a stuck running screen when startRun is refused (e.g. a 409)", async () => {
    mockStartRun.mockRejectedValue(new Error("409 a run is already active for this project"));

    await selectFishingTheme();
    fireEvent.click(screen.getByRole("button", { name: "Upload 5 items to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("409 a run is already active for this project");
    expect(screen.queryByRole("log")).not.toBeInTheDocument();
    expect(mockOpenOutput).not.toHaveBeenCalled();
  });

  it("shows the just-run theme's row breakdown on the finished screen", async () => {
    let capturedHandlers: OutputHandlers | undefined;
    mockOpenOutput.mockImplementation((handlers: OutputHandlers) => {
      capturedHandlers = handlers;
      return { close: vi.fn() };
    });
    // After the run, re-fetching the theme returns the uploaded row as done
    // plus one that still needs a title.
    const afterRunPreview: ValidateDoc = {
      ...PREVIEW,
      rows: [
        { row: 2, state: "done", verdict: "ready", identifier: "lcps-photosexample-00001", file: "photo1.jpg", errors: [], missing_fields: [] },
        { row: 3, state: "unassigned", verdict: "not_ready", identifier: "", file: "photo2.jpg", errors: [], missing_fields: ["title"] },
      ],
    };

    await selectFishingTheme();
    fireEvent.click(screen.getByRole("button", { name: "Upload 5 items to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(mockOpenOutput).toHaveBeenCalled());

    // The finished-screen re-fetch returns the post-run rows.
    mockGetPreview.mockResolvedValueOnce(afterRunPreview);
    act(() => {
      capturedHandlers?.onFinished(COMPLETED_ENDING);
    });

    expect(await screen.findByText("5 uploaded, 0 failed")).toBeInTheDocument();
    // The full breakdown appears without re-selecting the theme.
    expect(await screen.findByText("Uploaded (1)")).toBeInTheDocument();
    expect(screen.getByText("photo1.jpg")).toBeInTheDocument();
    expect(screen.getByText("Needs fixing")).toBeInTheDocument();
    expect(screen.getByText("row 3: needs title")).toBeInTheDocument();
  });

  it("resubscribes to the output stream when mounting mid-run, with no Confirm click involved", async () => {
    let capturedHandlers: OutputHandlers | undefined;
    mockOpenOutput.mockImplementation((handlers: OutputHandlers) => {
      capturedHandlers = handlers;
      return { close: vi.fn() };
    });
    mockGetStatus.mockResolvedValue(STATUS_RUNNING_MID_UPLOAD);

    render(<App />);

    await waitFor(() => expect(mockOpenOutput).toHaveBeenCalledTimes(1));
    expect(mockOpenOutput).toHaveBeenCalledWith(expect.anything(), 0);
    expect(await screen.findByRole("log")).toBeInTheDocument();
    expect(screen.getByText("2 of 5")).toBeInTheDocument();

    // A progress event reaching this fresh subscription updates the UI,
    // including naming the image now in flight.
    act(() => {
      capturedHandlers?.onProgress({ done: 3, planned: 5, current: { index: 4, file: "photos/x.jpg" } });
    });
    expect(await screen.findByText("3 of 5")).toBeInTheDocument();
    expect(screen.getByText(/Uploading item 4 of 5/)).toBeInTheDocument();

    // Stop still works on a run this page never itself started.
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    await waitFor(() => expect(mockStopRun).toHaveBeenCalledTimes(1));

    act(() => {
      capturedHandlers?.onFinished(COMPLETED_ENDING);
    });
    expect(await screen.findByText("5 uploaded, 0 failed")).toBeInTheDocument();
  });

  it("ignores a stored offset that belongs to a different run when mounting mid-run", async () => {
    // A stale offset left over from an earlier run of the same batch must not
    // be applied to this run's per-run output.txt - the resume key is the
    // run's started_at, so a mismatch falls back to a full replay (offset 0).
    window.sessionStorage.setItem(
      "upload-page:output-offset",
      JSON.stringify({ startedAt: "2000-01-01T00:00:00Z", byteOffset: 9999 }),
    );
    mockOpenOutput.mockImplementation(() => ({ close: vi.fn() }));
    mockGetStatus.mockResolvedValue(STATUS_RUNNING_MID_UPLOAD);

    render(<App />);

    await waitFor(() => expect(mockOpenOutput).toHaveBeenCalledTimes(1));
    expect(mockOpenOutput).toHaveBeenCalledWith(expect.anything(), 0);
  });

  it("resumes from a stored offset that belongs to this same run", async () => {
    // STATUS_RUNNING_MID_UPLOAD's run started at 2026-09-25T12:00:00Z.
    window.sessionStorage.setItem(
      "upload-page:output-offset",
      JSON.stringify({ startedAt: "2026-09-25T12:00:00Z", byteOffset: 4096 }),
    );
    mockOpenOutput.mockImplementation(() => ({ close: vi.fn() }));
    mockGetStatus.mockResolvedValue(STATUS_RUNNING_MID_UPLOAD);

    render(<App />);

    await waitFor(() => expect(mockOpenOutput).toHaveBeenCalledTimes(1));
    expect(mockOpenOutput).toHaveBeenCalledWith(expect.anything(), 4096);
  });

  it("shows the Finished screen once the output stream reports the run ended", async () => {
    let capturedHandlers: OutputHandlers | undefined;
    mockOpenOutput.mockImplementation((handlers: OutputHandlers) => {
      capturedHandlers = handlers;
      return { close: vi.fn() };
    });

    await selectFishingTheme();
    fireEvent.click(screen.getByRole("button", { name: "Upload 5 items to Internet Archive" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm" }));
    await screen.findByRole("log");

    act(() => {
      capturedHandlers?.onFinished(COMPLETED_ENDING);
    });

    expect(await screen.findByText("5 uploaded, 0 failed")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /choose a theme/i })).toBeInTheDocument();
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

  it("a finished leftover at load shows the picker, not a stale run summary", async () => {
    // /api/status is stateless and reports the newest page-run folder as
    // "finished" even on a fresh visit; that stale summary must not show -
    // start on the picker instead (a run started this session still shows its
    // own result when it completes).
    mockGetStatus.mockResolvedValue({
      live: false,
      project: "astoriaphotos",
      collection: "sarasoldphotos",
      version: "1.0.0",
      run: { kind: "finished", ending: COMPLETED_ENDING, page_run: null },
    });

    render(<App />);

    expect(await screen.findByRole("combobox", { name: /choose a theme/i })).toBeInTheDocument();
    expect(screen.queryByText("5 uploaded, 0 failed")).not.toBeInTheDocument();
  });

  it("picks a theme at load without re-fetching the stateless status (regression)", async () => {
    // A finished leftover at load starts on the picker (its stale summary is
    // suppressed); picking a theme must go straight to that theme's preview and
    // never re-fetch /api/status, which would keep reporting "finished".
    mockGetStatus.mockResolvedValue({
      live: false,
      project: "astoriaphotos",
      collection: "sarasoldphotos",
      version: "1.0.0",
      run: { kind: "finished", ending: COMPLETED_ENDING, page_run: null },
    });

    render(<App />);
    const trigger = await screen.findByRole("combobox", { name: /choose a theme/i });
    await waitFor(() => expect(trigger).toBeEnabled());
    fireEvent.click(trigger);
    fireEvent.click(await screen.findByRole("option", { name: /Fishing/ }));

    // Straight to that theme's preview - never back through the status fetch.
    await screen.findByText("5 ready to upload");
    expect(mockGetStatus).toHaveBeenCalledTimes(1);
  });
});
