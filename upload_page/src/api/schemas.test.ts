import { describe, expect, test } from "vitest";
import all from "../../../contract_fixtures/validate-all.json";
import batch from "../../../contract_fixtures/validate-batch.json";
import {
  BoundaryParseError,
  Ending,
  Health,
  RunState,
  SseFinishedEvent,
  SseProgressEvent,
  Status,
  Summary,
  ValidateDoc,
  parseBoundary,
} from "./schemas";

// A summary shaped like a real run_summary JSONL record, minus the extra
// record/timestamp/live bookkeeping fields page_runs.py forwards verbatim
// (zod drops those silently - see schemas.ts's comment on Summary).
const SAMPLE_SUMMARY: Summary = {
  attempted: 5,
  succeeded: 4,
  failures: [{ identifier: "lcps-astoriaphotos-00010", error: "S3 put failed" }],
  unconfirmed: [],
  not_attempted: 0,
  rate_limited: false,
  rate_limit_status: null,
  stopped_by_request: false,
  skipped: [{ identifier: "", error: "failed validation" }],
};

describe("ValidateDoc", () => {
  test("parses validate-all.json (batches present, rows null)", () => {
    const doc = ValidateDoc.parse(all);
    expect(doc.batches?.length).toBe(2);
    expect(doc.rows).toBeNull();
  });

  test("parses validate-batch.json (rows present, batches null)", () => {
    const doc = ValidateDoc.parse(batch);
    expect(doc.rows?.[0].state).toBe("unassigned");
    expect(doc.batches).toBeNull();
  });

  test("rejects an unknown verdict", () => {
    const withBadVerdict = {
      ...batch,
      rows: [{ ...batch.rows[0], verdict: "??" }],
    };
    expect(() => ValidateDoc.parse(withBadVerdict)).toThrow();
  });

  test("rejects an unknown row state", () => {
    const withBadState = {
      ...batch,
      rows: [{ ...batch.rows[0], state: "archived" }],
    };
    expect(() => ValidateDoc.parse(withBadState)).toThrow();
  });
});

describe("Health", () => {
  test("parses a sample health document", () => {
    const health = Health.parse({
      commit: "abc123",
      bundle_stamp: "deadbeef",
      live: false,
      project: "astoriaphotos",
    });
    expect(health).toEqual({
      commit: "abc123",
      bundle_stamp: "deadbeef",
      live: false,
      project: "astoriaphotos",
    });
  });
});

describe("RunState", () => {
  test("parses the idle kind", () => {
    expect(RunState.parse({ kind: "idle" })).toEqual({ kind: "idle" });
  });

  test("parses the page_run_active kind", () => {
    const state = RunState.parse({
      kind: "page_run_active",
      batch: "Fishing",
      live: false,
      started_at: "2026-09-25T12:00:00Z",
      done: 3,
      planned: 10,
    });
    expect(state).toMatchObject({ kind: "page_run_active", done: 3, planned: 10 });
  });

  test("parses the terminal_run_active kind with a holder", () => {
    const state = RunState.parse({
      kind: "terminal_run_active",
      holder: {
        started_at: "2026-09-25T12:00:00Z",
        project: "astoriaphotos",
        batch: "Logging",
        live: true,
      },
    });
    expect(state).toMatchObject({ kind: "terminal_run_active" });
    if (state.kind !== "terminal_run_active") throw new Error("unreachable");
    expect(state.holder?.project).toBe("astoriaphotos");
  });

  test("parses the terminal_run_active kind with no holder", () => {
    const state = RunState.parse({ kind: "terminal_run_active", holder: null });
    expect(state).toEqual({ kind: "terminal_run_active", holder: null });
  });

  test("parses the finished kind", () => {
    const state = RunState.parse({
      kind: "finished",
      ending: { kind: "completed", summary: SAMPLE_SUMMARY },
      page_run: {
        pid: 4242,
        project: "astoriaphotos",
        batch: "Fishing",
        live: false,
        started_at: "2026-09-25T12:00:00Z",
      },
    });
    expect(state.kind).toBe("finished");
    if (state.kind !== "finished") throw new Error("unreachable");
    expect(state.ending.kind).toBe("completed");
    expect(state.page_run?.pid).toBe(4242);
  });

  test("rejects an unknown kind", () => {
    expect(() => RunState.parse({ kind: "bogus" })).toThrow();
  });
});

describe("Ending", () => {
  test("parses the completed kind", () => {
    const ending = Ending.parse({ kind: "completed", summary: SAMPLE_SUMMARY });
    expect(ending).toEqual({ kind: "completed", summary: SAMPLE_SUMMARY });
  });

  test("parses the stopped kind", () => {
    const ending = Ending.parse({ kind: "stopped", summary: SAMPLE_SUMMARY, planned: 20 });
    expect(ending).toEqual({ kind: "stopped", summary: SAMPLE_SUMMARY, planned: 20 });
  });

  test("parses the rate_limited kind", () => {
    const ending = Ending.parse({ kind: "rate_limited", summary: SAMPLE_SUMMARY });
    expect(ending).toEqual({ kind: "rate_limited", summary: SAMPLE_SUMMARY });
  });

  test("parses the ended_without_summary kind", () => {
    expect(Ending.parse({ kind: "ended_without_summary" })).toEqual({ kind: "ended_without_summary" });
  });

  test("parses the refused kind", () => {
    const ending = Ending.parse({ kind: "refused", reason_lines: ["boom", "traceback line"] });
    expect(ending).toEqual({ kind: "refused", reason_lines: ["boom", "traceback line"] });
  });

  test("drops the extra record/timestamp/live fields a real JSONL summary carries", () => {
    const ending = Ending.parse({
      kind: "completed",
      summary: { ...SAMPLE_SUMMARY, record: "run_summary", timestamp: "2026-09-25T12:00:00Z", live: false },
    });
    expect(ending).toEqual({ kind: "completed", summary: SAMPLE_SUMMARY });
  });
});

describe("Status", () => {
  test("parses a sample status document", () => {
    const status = Status.parse({
      live: false,
      project: "astoriaphotos",
      collection: "sarasoldphotos",
      run: { kind: "idle" },
    });
    expect(status).toEqual({
      live: false,
      project: "astoriaphotos",
      collection: "sarasoldphotos",
      run: { kind: "idle" },
    });
  });
});

describe("SSE event payloads", () => {
  test("parses a progress event", () => {
    expect(SseProgressEvent.parse({ done: 3, planned: 10 })).toEqual({ done: 3, planned: 10 });
  });

  test("parses a progress event with an unknown planned total", () => {
    expect(SseProgressEvent.parse({ done: 3, planned: null })).toEqual({ done: 3, planned: null });
  });

  test("parses a finished event, unwrapping to its Ending", () => {
    const parsed = SseFinishedEvent.parse({
      ending: { kind: "completed", summary: SAMPLE_SUMMARY },
    });
    expect(parsed.ending).toEqual({ kind: "completed", summary: SAMPLE_SUMMARY });
  });
});

describe("parseBoundary", () => {
  test("returns the parsed value on success", () => {
    expect(parseBoundary(Health, { commit: "a", bundle_stamp: "b", live: true, project: "p" }, "test")).toEqual({
      commit: "a",
      bundle_stamp: "b",
      live: true,
      project: "p",
    });
  });

  test("throws a BoundaryParseError labeled with its context on failure", () => {
    expect(() => parseBoundary(Health, {}, "GET /api/health")).toThrow(BoundaryParseError);
    try {
      parseBoundary(Health, {}, "GET /api/health");
      throw new Error("expected parseBoundary to throw");
    } catch (error) {
      expect(error).toBeInstanceOf(BoundaryParseError);
      expect((error as Error).message).toContain("GET /api/health");
    }
  });
});
