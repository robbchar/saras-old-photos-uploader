import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BoundaryParseError } from "./schemas";
import { getHealth, getPreview, getStatus, getThemes, openOutput, startRun, stopRun } from "./client";

function jsonResponse(body: unknown, init: { ok?: boolean; status?: number } = {}): Response {
  const ok = init.ok ?? true;
  const status = init.status ?? (ok ? 200 : 500);
  return {
    ok,
    status,
    statusText: ok ? "OK" : "Error",
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

const SAMPLE_HEALTH = { commit: "abc123", bundle_stamp: "deadbeef", live: false, project: "astoriaphotos" };
const SAMPLE_STATUS = {
  live: false,
  project: "astoriaphotos",
  collection: "sarasoldphotos",
  run: { kind: "idle" },
};
const SAMPLE_VALIDATE_DOC = {
  format: 1,
  project: "astoriaphotos",
  live: false,
  batch: null,
  valid: true,
  sheet_errors: [],
  rows_with_errors: [],
  ready_to_upload: 0,
  counts: {
    unassigned: { ready: 0, invalid: 0, not_ready: 0 },
    done: { ready: 0, invalid: 0, not_ready: 0 },
    reserved: { ready: 0, invalid: 0, not_ready: 0 },
  },
  batches: [],
  rows: null,
};

describe("REST client methods", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("getHealth GETs /api/health and returns the parsed document", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SAMPLE_HEALTH));

    const health = await getHealth();

    expect(fetchMock).toHaveBeenCalledWith("/api/health");
    expect(health).toEqual(SAMPLE_HEALTH);
  });

  it("getStatus GETs /api/status and returns the parsed document", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SAMPLE_STATUS));

    const status = await getStatus();

    expect(fetchMock).toHaveBeenCalledWith("/api/status");
    expect(status).toEqual(SAMPLE_STATUS);
  });

  it("getThemes GETs /api/themes", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(SAMPLE_VALIDATE_DOC));

    const doc = await getThemes();

    expect(fetchMock).toHaveBeenCalledWith("/api/themes");
    expect(doc).toEqual(SAMPLE_VALIDATE_DOC);
  });

  it("getPreview GETs /api/preview with the batch query-encoded", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ ...SAMPLE_VALIDATE_DOC, batch: "Fishing & Boats" }));

    await getPreview("Fishing & Boats");

    expect(fetchMock).toHaveBeenCalledWith("/api/preview?batch=Fishing%20%26%20Boats");
  });

  it("startRun POSTs to /api/runs with a JSON content-type and the batch body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ started_at: "2026-09-25T12:00:00Z" }, { status: 202 }));

    const result = await startRun("Fishing");

    expect(fetchMock).toHaveBeenCalledWith("/api/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch: "Fishing" }),
    });
    expect(result).toEqual({ started_at: "2026-09-25T12:00:00Z" });
  });

  it("stopRun POSTs an empty JSON body to /api/runs/current/stop", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}, { status: 202 }));

    await stopRun();

    expect(fetchMock).toHaveBeenCalledWith("/api/runs/current/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
  });

  it("rejects when the server responds with a non-ok status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ error: "boom" }, { ok: false, status: 500 }));

    await expect(getHealth()).rejects.toThrow(/GET \/api\/health failed: 500/);
  });

  it("rejects with a BoundaryParseError when the response does not match the schema", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ not: "a health document" }));

    await expect(getHealth()).rejects.toBeInstanceOf(BoundaryParseError);
  });
});

// EventSource is not implemented by jsdom, so a minimal EventTarget-based
// stand-in is stubbed in for these tests - just enough to (a) record the
// URL openOutput constructed it with, and (b) let tests dispatch fake
// server events at it.
class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = [];
  // Mirrors the real EventSource readyState constants used by client.ts.
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 2;
  readonly url: string;
  // OPEN by default, like a real connected EventSource.
  readyState: number = FakeEventSource.OPEN;

  constructor(url: string) {
    super();
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  close(): void {
    this.readyState = FakeEventSource.CLOSED;
  }
}

describe("openOutput", () => {
  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("connects to the output route with no query string when fromOffset is omitted", () => {
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished: vi.fn(), onError: vi.fn() });

    expect(FakeEventSource.instances).toHaveLength(1);
    expect(FakeEventSource.instances[0].url).toBe("/api/runs/current/output");
  });

  it("connects with ?from=<offset> when fromOffset is given", () => {
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished: vi.fn(), onError: vi.fn() }, 517);

    expect(FakeEventSource.instances[0].url).toBe("/api/runs/current/output?from=517");
  });

  it("forwards a line event's text and byte offset (lastEventId) to onLine", () => {
    const onLine = vi.fn();
    openOutput({ onLine, onProgress: vi.fn(), onFinished: vi.fn(), onError: vi.fn() });
    const source = FakeEventSource.instances[0];

    source.dispatchEvent(new MessageEvent("line", { data: "uploading photo1.jpg", lastEventId: "128" }));

    expect(onLine).toHaveBeenCalledWith("uploading photo1.jpg", 128);
  });

  it("parses a progress event's JSON data and forwards it to onProgress", () => {
    const onProgress = vi.fn();
    openOutput({ onLine: vi.fn(), onProgress, onFinished: vi.fn(), onError: vi.fn() });
    const source = FakeEventSource.instances[0];

    source.dispatchEvent(new MessageEvent("progress", { data: JSON.stringify({ done: 3, planned: 10 }) }));

    expect(onProgress).toHaveBeenCalledWith({ done: 3, planned: 10 });
  });

  it("parses a finished event and forwards the unwrapped Ending to onFinished", () => {
    const onFinished = vi.fn();
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished, onError: vi.fn() });
    const source = FakeEventSource.instances[0];
    const ending = { kind: "completed", summary: {
      attempted: 1, succeeded: 1, failures: [], unconfirmed: [], not_attempted: 0,
      rate_limited: false, rate_limit_status: null, stopped_by_request: false, skipped: [],
    } };

    source.dispatchEvent(new MessageEvent("finished", { data: JSON.stringify({ ending }) }));

    expect(onFinished).toHaveBeenCalledWith(ending);
  });

  it("routes a malformed progress payload to onError instead of throwing", () => {
    const onError = vi.fn();
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished: vi.fn(), onError });
    const source = FakeEventSource.instances[0];

    source.dispatchEvent(new MessageEvent("progress", { data: JSON.stringify({ done: "not a number" }) }));

    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("routes a connection-level error event to onError", () => {
    const onError = vi.fn();
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished: vi.fn(), onError });
    const source = FakeEventSource.instances[0];

    source.dispatchEvent(new Event("error"));

    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("does not surface an error while the browser is auto-reconnecting (readyState CONNECTING)", () => {
    // A server restart mid-run drops the connection; the browser retries
    // on its own and resends Last-Event-ID, so this must not be treated
    // as a run-ending failure.
    const onError = vi.fn();
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished: vi.fn(), onError });
    const source = FakeEventSource.instances[0];
    source.readyState = FakeEventSource.CONNECTING;

    source.dispatchEvent(new Event("error"));

    expect(onError).not.toHaveBeenCalled();
  });

  it("surfaces an error once the connection is fully closed (readyState CLOSED)", () => {
    const onError = vi.fn();
    openOutput({ onLine: vi.fn(), onProgress: vi.fn(), onFinished: vi.fn(), onError });
    const source = FakeEventSource.instances[0];
    source.readyState = FakeEventSource.CLOSED;

    source.dispatchEvent(new Event("error"));

    expect(onError).toHaveBeenCalledTimes(1);
  });
});
