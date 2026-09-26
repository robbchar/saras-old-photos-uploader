import { describe, expect, test } from "vitest";
import { reducer } from "./reducer";
import type { Action, AppState } from "./types";
import type { Ending, RunState, ValidateDoc } from "../api/schemas";

// ---------------------------------------------------------------------
// Fixtures - minimal values satisfying the Task 11 wire types. These are
// plain object literals (not run through zod) since the reducer itself
// never parses anything; parsing already happened at the api/client.ts
// boundary before an action reaches here.
// ---------------------------------------------------------------------

const SAMPLE_VERDICTS = { ready: 1, invalid: 0, not_ready: 0 };
const SAMPLE_COUNTS = { unassigned: SAMPLE_VERDICTS, done: SAMPLE_VERDICTS, reserved: SAMPLE_VERDICTS };

const SAMPLE_PREVIEW: ValidateDoc = {
  format: 1,
  project: "astoriaphotos",
  live: false,
  batch: "Fishing",
  valid: true,
  sheet_errors: [],
  rows_with_errors: [],
  ready_to_upload: 7,
  counts: SAMPLE_COUNTS,
  batches: null,
  rows: [],
};

const SAMPLE_THEMES: ValidateDoc = {
  ...SAMPLE_PREVIEW,
  batch: null,
  batches: [],
  rows: null,
};

const SAMPLE_SUMMARY = {
  attempted: 5,
  succeeded: 5,
  failures: [],
  unconfirmed: [],
  not_attempted: 0,
  rate_limited: false,
  rate_limit_status: null,
  stopped_by_request: false,
  skipped: [],
};

const SAMPLE_ENDING: Ending = { kind: "completed", summary: SAMPLE_SUMMARY };

const SAMPLE_HOLDER = {
  started_at: "2026-09-25T12:00:00Z",
  project: "astoriaphotos",
  batch: "Fishing",
  live: false,
};

const CHECKED_AT = "2026-09-25T12:00:00Z";

// One representative AppState per `kind`. The mapped type below is what
// makes this exhaustive: adding a new AppState variant without adding a
// row here is a compile error, not a silently-skipped test case.
const sampleStates: { [K in AppState["kind"]]: Extract<AppState, { kind: K }> } = {
  loading: { kind: "loading" },
  choosing: { kind: "choosing", themes: SAMPLE_THEMES },
  checking: { kind: "checking", batch: "Fishing" },
  previewed: { kind: "previewed", batch: "Fishing", preview: SAMPLE_PREVIEW, checkedAt: CHECKED_AT },
  confirming: { kind: "confirming", batch: "Fishing", preview: SAMPLE_PREVIEW, checkedAt: CHECKED_AT },
  running: { kind: "running", batch: "Fishing", done: 2, planned: 7, current: null },
  stopping: { kind: "stopping", batch: "Fishing", done: 2, planned: 7, current: null },
  finished: { kind: "finished", ending: SAMPLE_ENDING },
  "terminal-run": { kind: "terminal-run", holder: SAMPLE_HOLDER },
  error: { kind: "error", message: "boom" },
};

// One representative Action per `type`. Where a type's real payload can
// route to several different destinations (status/received), this picks
// the "idle" case; the other routes get their own targeted tests below.
const sampleActions: { [T in Action["type"]]: Extract<Action, { type: T }> } = {
  "status/received": { type: "status/received", run: { kind: "idle" } },
  "themes/loaded": { type: "themes/loaded", themes: SAMPLE_THEMES },
  "theme/selected": { type: "theme/selected", batch: "Fishing" },
  "preview/loaded": { type: "preview/loaded", preview: SAMPLE_PREVIEW, checkedAt: CHECKED_AT },
  "preview/failed": { type: "preview/failed", message: "preview refused" },
  "recheck/clicked": { type: "recheck/clicked" },
  "start/clicked": { type: "start/clicked" },
  "confirm/cancel": { type: "confirm/cancel" },
  "confirm/yes": { type: "confirm/yes" },
  "sse/progress": { type: "sse/progress", done: 3, planned: 7, current: null },
  "stop/clicked": { type: "stop/clicked" },
  "sse/finished": { type: "sse/finished", ending: SAMPLE_ENDING },
  "choose-another/clicked": { type: "choose-another/clicked" },
  error: { type: "error", message: "boom" },
};

const allKinds = Object.keys(sampleStates) as Array<AppState["kind"]>;
const allActionTypes = Object.keys(sampleActions) as Array<Action["type"]>;

// The full set of defined transitions, using each action type's single
// representative payload above. `status/received`'s other three routes
// (page_run_active, terminal_run_active, finished) are only reachable
// from `loading` with a *different* payload than the representative one,
// so they get their own targeted tests instead of a row here.
const definedTransitions: ReadonlyArray<[AppState["kind"], Action["type"], AppState["kind"]]> = [
  ["loading", "status/received", "choosing"],
  ["choosing", "themes/loaded", "choosing"],
  ["choosing", "theme/selected", "checking"],
  ["checking", "preview/loaded", "previewed"],
  ["checking", "preview/failed", "error"],
  ["previewed", "recheck/clicked", "checking"],
  ["previewed", "start/clicked", "confirming"],
  ["confirming", "confirm/cancel", "previewed"],
  ["confirming", "confirm/yes", "running"],
  ["running", "sse/progress", "running"],
  ["running", "stop/clicked", "stopping"],
  ["running", "sse/finished", "finished"],
  ["stopping", "sse/progress", "stopping"],
  ["stopping", "sse/finished", "finished"],
  ["finished", "choose-another/clicked", "choosing"],
  // "error" is accepted from every state, including "error" itself.
  ...allKinds.map((kind): [AppState["kind"], Action["type"], AppState["kind"]] => [kind, "error", "error"]),
];

function transitionKey(kind: AppState["kind"], type: Action["type"]): string {
  return `${kind}::${type}`;
}

const definedTransitionKeys = new Set(definedTransitions.map(([kind, type]) => transitionKey(kind, type)));

describe("reducer - defined transitions", () => {
  test.each(definedTransitions)("%s --[%s]--> %s", (fromKind, actionType, toKind) => {
    const result = reducer(sampleStates[fromKind], sampleActions[actionType]);
    expect(result.kind).toBe(toKind);
  });
});

describe("reducer - impossible transitions leave state unchanged", () => {
  const impossiblePairs = allKinds.flatMap((kind) =>
    allActionTypes
      .filter((type) => !definedTransitionKeys.has(transitionKey(kind, type)))
      .map((type): [AppState["kind"], Action["type"]] => [kind, type]),
  );

  test("there is at least one impossible pair to check", () => {
    expect(impossiblePairs.length).toBeGreaterThan(0);
  });

  test.each(impossiblePairs)("%s + %s is a no-op", (kind, type) => {
    const from = sampleStates[kind];
    const result = reducer(from, sampleActions[type]);
    // Same reference, not just an equal value: an impossible action must
    // not allocate a new state object.
    expect(result).toBe(from);
  });
});

describe("status/received routes by run.kind (only from loading)", () => {
  test("idle -> choosing with themes not yet loaded", () => {
    const action: Action = { type: "status/received", run: { kind: "idle" } };
    const result = reducer(sampleStates.loading, action);
    expect(result).toEqual({ kind: "choosing", themes: null });
  });

  test("page_run_active -> running, carrying the run's batch/done/planned/current", () => {
    const run: RunState = {
      kind: "page_run_active",
      batch: "Logging",
      live: false,
      started_at: CHECKED_AT,
      done: 4,
      planned: 12,
      current: { index: 5, file: "e.jpg" },
    };
    const result = reducer(sampleStates.loading, { type: "status/received", run });
    expect(result).toEqual({
      kind: "running",
      batch: "Logging",
      done: 4,
      planned: 12,
      current: { index: 5, file: "e.jpg" },
    });
  });

  test("terminal_run_active -> terminal-run, carrying the holder", () => {
    const run: RunState = { kind: "terminal_run_active", holder: SAMPLE_HOLDER };
    const result = reducer(sampleStates.loading, { type: "status/received", run });
    expect(result).toEqual({ kind: "terminal-run", holder: SAMPLE_HOLDER });
  });

  test("terminal_run_active with no holder -> terminal-run, holder null", () => {
    const run: RunState = { kind: "terminal_run_active", holder: null };
    const result = reducer(sampleStates.loading, { type: "status/received", run });
    expect(result).toEqual({ kind: "terminal-run", holder: null });
  });

  test("finished -> finished, carrying the run's ending", () => {
    const run: RunState = { kind: "finished", ending: SAMPLE_ENDING, page_run: null };
    const result = reducer(sampleStates.loading, { type: "status/received", run });
    expect(result).toEqual({ kind: "finished", ending: SAMPLE_ENDING });
  });

  test("from a non-loading state, status/received is ignored", () => {
    const run: RunState = { kind: "finished", ending: SAMPLE_ENDING, page_run: null };
    const result = reducer(sampleStates.choosing, { type: "status/received", run });
    expect(result).toBe(sampleStates.choosing);
  });
});

describe("other data-carrying transitions", () => {
  test("themes/loaded sets themes on the choosing state", () => {
    const loading: AppState = { kind: "choosing", themes: null };
    const result = reducer(loading, { type: "themes/loaded", themes: SAMPLE_THEMES });
    expect(result).toEqual({ kind: "choosing", themes: SAMPLE_THEMES });
  });

  test("confirm/yes starts running at done:0, planned from the preview's ready_to_upload", () => {
    const confirming: AppState = {
      kind: "confirming",
      batch: "Fishing",
      preview: SAMPLE_PREVIEW,
      checkedAt: CHECKED_AT,
    };
    const result = reducer(confirming, { type: "confirm/yes" });
    expect(result).toEqual({
      kind: "running",
      batch: "Fishing",
      done: 0,
      planned: SAMPLE_PREVIEW.ready_to_upload,
      current: null,
    });
  });

  test("sse/progress updates done/planned/current while running", () => {
    const running: AppState = { kind: "running", batch: "Fishing", done: 1, planned: 7, current: null };
    const result = reducer(running, {
      type: "sse/progress",
      done: 5,
      planned: 7,
      current: { index: 6, file: "f.jpg" },
    });
    expect(result).toEqual({
      kind: "running",
      batch: "Fishing",
      done: 5,
      planned: 7,
      current: { index: 6, file: "f.jpg" },
    });
  });

  test("sse/progress updates done/planned/current while stopping", () => {
    const stopping: AppState = { kind: "stopping", batch: "Fishing", done: 1, planned: 7, current: null };
    const result = reducer(stopping, { type: "sse/progress", done: 6, planned: 7, current: null });
    expect(result).toEqual({ kind: "stopping", batch: "Fishing", done: 6, planned: 7, current: null });
  });

  test("stop/clicked carries the current batch/done/planned/current into stopping", () => {
    const running: AppState = {
      kind: "running",
      batch: "Fishing",
      done: 3,
      planned: 7,
      current: { index: 4, file: "d.jpg" },
    };
    const result = reducer(running, { type: "stop/clicked" });
    expect(result).toEqual({
      kind: "stopping",
      batch: "Fishing",
      done: 3,
      planned: 7,
      current: { index: 4, file: "d.jpg" },
    });
  });

  test("recheck/clicked returns to checking with the same batch", () => {
    const previewed: AppState = {
      kind: "previewed",
      batch: "Fishing",
      preview: SAMPLE_PREVIEW,
      checkedAt: CHECKED_AT,
    };
    const result = reducer(previewed, { type: "recheck/clicked" });
    expect(result).toEqual({ kind: "checking", batch: "Fishing" });
  });

  test("start/clicked opens the confirm dialog carrying the preview and checkedAt", () => {
    const previewed: AppState = {
      kind: "previewed",
      batch: "Fishing",
      preview: SAMPLE_PREVIEW,
      checkedAt: CHECKED_AT,
    };
    const result = reducer(previewed, { type: "start/clicked" });
    expect(result).toEqual({ kind: "confirming", batch: "Fishing", preview: SAMPLE_PREVIEW, checkedAt: CHECKED_AT });
  });

  test("confirm/cancel returns to previewed with the same preview and checkedAt", () => {
    const confirming: AppState = {
      kind: "confirming",
      batch: "Fishing",
      preview: SAMPLE_PREVIEW,
      checkedAt: CHECKED_AT,
    };
    const result = reducer(confirming, { type: "confirm/cancel" });
    expect(result).toEqual({ kind: "previewed", batch: "Fishing", preview: SAMPLE_PREVIEW, checkedAt: CHECKED_AT });
  });

  test("preview/failed goes to error carrying the message", () => {
    const checking: AppState = { kind: "checking", batch: "Fishing" };
    const result = reducer(checking, { type: "preview/failed", message: "sheet unreadable" });
    expect(result).toEqual({ kind: "error", message: "sheet unreadable" });
  });

  test("choose-another/clicked resets a finished run to a refreshed picker (themes not yet loaded)", () => {
    const finished: AppState = { kind: "finished", ending: SAMPLE_ENDING };
    const result = reducer(finished, { type: "choose-another/clicked" });
    expect(result).toEqual({ kind: "choosing", themes: null });
  });

  test("error is accepted from any state, carrying the message", () => {
    for (const kind of allKinds) {
      const result = reducer(sampleStates[kind], { type: "error", message: `boom from ${kind}` });
      expect(result).toEqual({ kind: "error", message: `boom from ${kind}` });
    }
  });
});
