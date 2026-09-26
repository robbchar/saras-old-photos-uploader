// Zod v4 boundary schemas for every JSON shape upload_server.py sends this
// page.
//
// Every response from the server arrives as `unknown` (fetch's `.json()`
// return type, or a parsed SSE `data:` payload) and MUST be parsed here
// before anything downstream treats it as typed data - no casts. Field
// names below match the server's JSON exactly (snake_case), even though
// the rest of this codebase is camelCase, because these are wire shapes,
// not our own vocabulary.
//
// Several schemas below export both a value and a type of the same name
// (`export const Foo = z.object(...)` + `export type Foo = z.infer<typeof
// Foo>`). That is a deliberate zod convention, not a naming accident:
// `Foo.parse(x)` uses the value, `x: Foo` uses the type, and callers pick
// whichever the position calls for.

import { z } from "zod";

// ---------------------------------------------------------------------
// validate --json (ValidateDoc) - the theme/preview document.
// Schema shapes below mirror the brief's pinned zod v4 snippet exactly;
// `Verdicts`/`Counts`/`Batch`/`Row` are internal building blocks (not
// exported by those generic names - see the exported type aliases below
// instead, which won't collide with an unrelated same-named export later).
// ---------------------------------------------------------------------

const Verdicts = z.object({
  ready: z.number(),
  invalid: z.number(),
  not_ready: z.number(),
});

const Counts = z.object({
  unassigned: Verdicts,
  done: Verdicts,
  reserved: Verdicts,
});

const Batch = z.object({
  value: z.string(),
  ready_to_upload: z.number(),
  counts: Counts,
});

const Row = z.object({
  row: z.number(),
  state: z.enum(["unassigned", "done", "reserved"]),
  verdict: z.enum(["ready", "invalid", "not_ready"]),
  identifier: z.string(),
  errors: z.array(z.string()),
  missing_fields: z.array(z.string()),
});

// "all themes" mode (batches populated, rows null) vs. "one theme's rows"
// mode (rows populated, batches null) - see ia_bulk.py's `validate --json`.
export const ValidateDoc = z.object({
  format: z.literal(1),
  project: z.string(),
  live: z.boolean(),
  batch: z.string().nullable(),
  valid: z.boolean(),
  sheet_errors: z.array(z.string()),
  rows_with_errors: z.array(z.number()),
  ready_to_upload: z.number(),
  counts: Counts,
  batches: z.array(Batch).nullable(),
  rows: z.array(Row).nullable(),
});
export type ValidateDoc = z.infer<typeof ValidateDoc>;

// Exported under clearer names than the internal `Row`/`Batch`/`Verdicts`
// building blocks above, for components (Tasks 13/14) that need to type a
// single row or batch rather than the whole document.
export type ValidateRow = z.infer<typeof Row>;
export type ValidateBatch = z.infer<typeof Batch>;
export type VerdictCounts = z.infer<typeof Verdicts>;
export type ValidateCounts = z.infer<typeof Counts>;

// ---------------------------------------------------------------------
// GET /api/health
// ---------------------------------------------------------------------

export const Health = z.object({
  commit: z.string(),
  bundle_stamp: z.string(),
  live: z.boolean(),
  project: z.string(),
});
export type Health = z.infer<typeof Health>;

// ---------------------------------------------------------------------
// RunState / Ending - mirror page_runs.py's to_json() output exactly.
// Ending is defined first: RunState's "finished" variant embeds it.
// ---------------------------------------------------------------------

// The same shape serves both ways a row can miss an upload (see
// ia_bulk.py's RowFailure docstring) - a send Internet Archive refused,
// and a row a run declined to send at all.
const RowFailure = z.object({
  identifier: z.string(),
  error: z.string(),
});
export type RowFailure = z.infer<typeof RowFailure>;

// The run_summary JSONL record page_runs.read_ending() forwards as-is
// also carries a few bookkeeping fields (record, timestamp, live) that
// the page has no use for; zod's default object parsing drops unlisted
// keys silently, so they are intentionally not modeled here.
export const Summary = z.object({
  attempted: z.number(),
  succeeded: z.number(),
  failures: z.array(RowFailure),
  unconfirmed: z.array(RowFailure),
  not_attempted: z.number(),
  rate_limited: z.boolean(),
  rate_limit_status: z.number().nullable(),
  stopped_by_request: z.boolean(),
  skipped: z.array(RowFailure),
});
export type Summary = z.infer<typeof Summary>;

const Completed = z.object({
  kind: z.literal("completed"),
  summary: Summary,
});

const Stopped = z.object({
  kind: z.literal("stopped"),
  summary: Summary,
  planned: z.number().nullable(),
});

const RateLimited = z.object({
  kind: z.literal("rate_limited"),
  summary: Summary,
});

const EndedWithoutSummary = z.object({
  kind: z.literal("ended_without_summary"),
});

const Refused = z.object({
  kind: z.literal("refused"),
  reason_lines: z.array(z.string()),
});

export const Ending = z.discriminatedUnion("kind", [
  Completed,
  Stopped,
  RateLimited,
  EndedWithoutSummary,
  Refused,
]);
export type Ending = z.infer<typeof Ending>;

const LockHolder = z.object({
  started_at: z.string(),
  project: z.string(),
  batch: z.string().nullable(),
  live: z.boolean(),
});

const Idle = z.object({
  kind: z.literal("idle"),
});

// The item a run is uploading right now (mirrors page_runs.py's CurrentItem).
// Both PageRunActive and the SSE progress event carry it; null when nothing is
// in flight - between items, or before the first has started.
const CurrentItem = z.object({
  index: z.number(),
  file: z.string(),
});
export type CurrentItem = z.infer<typeof CurrentItem>;

const PageRunActive = z.object({
  kind: z.literal("page_run_active"),
  batch: z.string(),
  live: z.boolean(),
  started_at: z.string(),
  done: z.number(),
  planned: z.number().nullable(),
  current: CurrentItem.nullable(),
});

const TerminalRunActive = z.object({
  kind: z.literal("terminal_run_active"),
  holder: LockHolder.nullable(),
});

// The page_run a Finished run-state carries is PageRun.to_json() (Task 8's
// page_runs.PageRun), NOT the same shape as PageRunActive above.
const FinishedPageRun = z.object({
  pid: z.number(),
  project: z.string(),
  batch: z.string(),
  live: z.boolean(),
  started_at: z.string(),
});

const Finished = z.object({
  kind: z.literal("finished"),
  ending: Ending,
  page_run: FinishedPageRun.nullable(),
});

export const RunState = z.discriminatedUnion("kind", [
  Idle,
  PageRunActive,
  TerminalRunActive,
  Finished,
]);
export type RunState = z.infer<typeof RunState>;

// ---------------------------------------------------------------------
// GET /api/status
// ---------------------------------------------------------------------

export const Status = z.object({
  live: z.boolean(),
  project: z.string(),
  collection: z.string(),
  run: RunState,
});
export type Status = z.infer<typeof Status>;

// ---------------------------------------------------------------------
// POST /api/runs response body
// ---------------------------------------------------------------------

export const StartRunResponse = z.object({
  started_at: z.string(),
});
export type StartRunResponse = z.infer<typeof StartRunResponse>;

// ---------------------------------------------------------------------
// SSE event payloads (GET /api/runs/current/output).
//
// Named Sse*Event rather than bare ProgressEvent/FinishedEvent so they
// never shadow the DOM lib's own global `ProgressEvent` type, which
// client.ts (or a future component) may separately need for real browser
// events.
// ---------------------------------------------------------------------

export const SseProgressEvent = z.object({
  done: z.number(),
  planned: z.number().nullable(),
  current: CurrentItem.nullable(),
});
export type SseProgressEvent = z.infer<typeof SseProgressEvent>;

// The server wraps the ending object under "ending" for this event only
// (see upload_server.py's _stream_output: `{"ending": ending.to_json()}`).
export const SseFinishedEvent = z.object({
  ending: Ending,
});
export type SseFinishedEvent = z.infer<typeof SseFinishedEvent>;

// ---------------------------------------------------------------------
// Boundary parsing
// ---------------------------------------------------------------------

/**
 * Thrown when a server response does not match its expected schema.
 * `client.ts` throws this (never a raw `ZodError`) for every parsed
 * response, so callers only ever need to handle one error shape at this
 * boundary.
 */
export class BoundaryParseError extends Error {
  constructor(context: string, issues: readonly z.core.$ZodIssue[]) {
    const detail = issues
      .map((issue) => `${issue.path.join(".") || "(root)"}: ${issue.message}`)
      .join("; ");
    super(`${context}: response did not match the expected shape (${detail})`);
    this.name = "BoundaryParseError";
  }
}

/**
 * Parses `data` with `schema`, or throws a `BoundaryParseError` labeled
 * with `context` (e.g. "GET /api/health"). The one place every server
 * response - REST or SSE - is funneled through before anything trusts its
 * shape.
 */
export function parseBoundary<Schema extends z.ZodType>(
  schema: Schema,
  data: unknown,
  context: string,
): z.infer<Schema> {
  const result = schema.safeParse(data);
  if (!result.success) {
    throw new BoundaryParseError(context, result.error.issues);
  }
  return result.data;
}
