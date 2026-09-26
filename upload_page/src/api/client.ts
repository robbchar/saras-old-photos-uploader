// Fetch + SSE client for upload_server.py's routes. Every function parses
// the server's response through schemas.ts before returning it - nothing
// here casts an `unknown` payload into a typed one.

import {
  Ending,
  Health,
  SseFinishedEvent,
  SseProgressEvent,
  StartRunResponse,
  Status,
  ValidateDoc,
  parseBoundary,
} from "./schemas";

const JSON_HEADERS: HeadersInit = { "Content-Type": "application/json" };

async function assertOk(response: Response, context: string): Promise<void> {
  if (!response.ok) {
    throw new Error(`${context} failed: ${response.status} ${response.statusText}`);
  }
}

/** GETs `url`, parses the response as JSON, and returns it unvalidated
 * (the caller runs it through `parseBoundary` with the right schema). */
async function getJson(url: string, context: string): Promise<unknown> {
  const response = await fetch(url);
  await assertOk(response, context);
  return response.json();
}

/** POSTs `body` as `application/json` to `url` and returns the parsed
 * response body, unvalidated. */
async function postJson(url: string, body: unknown, context: string): Promise<unknown> {
  const response = await fetch(url, {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify(body),
  });
  await assertOk(response, context);
  return response.json();
}

export async function getHealth(): Promise<Health> {
  const context = "GET /api/health";
  const data = await getJson("/api/health", context);
  return parseBoundary(Health, data, context);
}

export async function getStatus(): Promise<Status> {
  const context = "GET /api/status";
  const data = await getJson("/api/status", context);
  return parseBoundary(Status, data, context);
}

export async function getThemes(): Promise<ValidateDoc> {
  const context = "GET /api/themes";
  const data = await getJson("/api/themes", context);
  return parseBoundary(ValidateDoc, data, context);
}

export async function getPreview(batch: string): Promise<ValidateDoc> {
  const context = "GET /api/preview";
  const data = await getJson(`/api/preview?batch=${encodeURIComponent(batch)}`, context);
  return parseBoundary(ValidateDoc, data, context);
}

export async function startRun(batch: string): Promise<StartRunResponse> {
  const context = "POST /api/runs";
  const data = await postJson("/api/runs", { batch }, context);
  return parseBoundary(StartRunResponse, data, context);
}

export async function stopRun(): Promise<void> {
  const response = await fetch("/api/runs/current/stop", {
    method: "POST",
    headers: JSON_HEADERS,
    body: JSON.stringify({}),
  });
  await assertOk(response, "POST /api/runs/current/stop");
}

// ---------------------------------------------------------------------
// GET /api/runs/current/output (SSE)
// ---------------------------------------------------------------------

export interface OutputHandlers {
  /** One per complete line of the run's console output. `byteOffset` is
   * the event's `lastEventId` (the cumulative byte offset in output.txt
   * right after this line's newline) - pass it back into `openOutput`'s
   * `fromOffset` to resume after a page reload. */
  onLine: (text: string, byteOffset: number) => void;
  onProgress: (progress: SseProgressEvent) => void;
  onFinished: (ending: Ending) => void;
  /** A connection-level error (a real DOM `Event` from EventSource) or a
   * boundary parse failure on a `progress`/`finished` payload. */
  onError: (error: unknown) => void;
}

/** Named SSE events the server writes; see upload_server.py's
 * _write_sse_event calls in _stream_output/_drain_output_lines. */
type OutputEventName = "line" | "progress" | "finished";

function asMessageEvent(event: Event): MessageEvent<string> {
  if (!(event instanceof MessageEvent)) {
    throw new Error(`expected a MessageEvent for SSE event "${event.type}"`);
  }
  return event;
}

/**
 * Opens the run's output stream. `EventSource` can only send the
 * `Last-Event-ID` header on its own automatic reconnects, never on the
 * first connect - so an explicit resume (reopening the stream after a
 * page reload) is asked for with `?from=<byteOffset>` instead, which the
 * server honors identically. Omit `fromOffset` (or pass 0) to start from
 * the beginning, same as a fresh page load.
 *
 * Returns the underlying `EventSource` so the caller can `.close()` it
 * (e.g. from a `useEffect` cleanup).
 */
export function openOutput(handlers: OutputHandlers, fromOffset?: number): EventSource {
  const url = "/api/runs/current/output" + (fromOffset ? "?from=" + fromOffset : "");
  const source = new EventSource(url);

  const addListener = (name: OutputEventName, listener: (event: MessageEvent<string>) => void) => {
    source.addEventListener(name, (event) => listener(asMessageEvent(event)));
  };

  addListener("line", (event) => {
    handlers.onLine(event.data, Number(event.lastEventId));
  });

  addListener("progress", (event) => {
    try {
      const data: unknown = JSON.parse(event.data);
      handlers.onProgress(parseBoundary(SseProgressEvent, data, "SSE progress event"));
    } catch (error) {
      handlers.onError(error);
    }
  });

  addListener("finished", (event) => {
    try {
      const data: unknown = JSON.parse(event.data);
      const finished = parseBoundary(SseFinishedEvent, data, "SSE finished event");
      handlers.onFinished(finished.ending);
    } catch (error) {
      handlers.onError(error);
    }
  });

  source.addEventListener("error", (event) => {
    handlers.onError(event);
  });

  return source;
}
