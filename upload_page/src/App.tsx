// Wires the state machine (state/reducer.ts) to the outside world: the
// REST/SSE client (api/client.ts) and the browser (timers, EventSource,
// window.location). Every other component in src/components is purely
// presentational - this is the one place that performs I/O and the one
// place that dispatches the Actions those side effects produce.

import { useEffect, useReducer, useRef, useState } from "react";
import {
  getHealth,
  getPreview,
  getStatus,
  getThemes,
  openOutput,
  startRun,
  stopRun,
  type OutputHandlers,
} from "./api/client";
import type { AppState, TerminalRunHolder } from "./state/types";
import type { ValidateDoc, ValidateRow } from "./api/schemas";
import { reducer } from "./state/reducer";
import { Header } from "./components/Header";
import { ThemePicker } from "./components/ThemePicker";
import { Preview } from "./components/Preview";
import { StartDialog } from "./components/StartDialog";
import { RunningOutput } from "./components/RunningOutput";
import { Finished } from "./components/Finished";
import { LiveRegion } from "./components/LiveRegion";

/** How often the page checks whether a newer build has been deployed. A
 * fresh `bundle_stamp` means the server was restarted with new code - the
 * only fix is a hard reload, since this is a single-page app with no
 * client-side update mechanism of its own. Exported so App.test.tsx drives
 * its fake timers off the same number instead of a hand-mirrored copy. */
export const HEALTH_POLL_INTERVAL_MS = 10_000;

/** How many of the most recent output lines to keep on screen. A run over
 * ~10,000 photos can write far more lines than anyone will ever scroll
 * back to; capping the buffer keeps both the array and the DOM it renders
 * into bounded, without changing what the reader sees at the bottom. */
const MAX_BUFFERED_LINES = 2000;

/** Appends `line`, dropping the oldest line once already at the cap so the
 * buffer never grows past MAX_BUFFERED_LINES. Exported for direct testing. */
export function appendLineCapped(previous: string[], line: string): string[] {
  return previous.length < MAX_BUFFERED_LINES ? [...previous, line] : [...previous.slice(1), line];
}

interface PageIdentity {
  project: string;
  collection: string;
  live: boolean;
}

// Last byte offset shown for a run, keyed by the run's `started_at`, so a
// reload can resume the output stream instead of replaying it from the start.
// Keyed by the run identity, NOT the batch: output.txt is per-run, so a stale
// offset left over from an earlier run of the same batch must never be applied
// to a different run's file.
const OUTPUT_OFFSET_STORAGE_KEY = "upload-page:output-offset";

interface StoredOutputOffset {
  startedAt: string;
  byteOffset: number;
}

function readStoredOffset(startedAt: string): number {
  try {
    const raw = window.sessionStorage.getItem(OUTPUT_OFFSET_STORAGE_KEY);
    if (!raw) return 0;
    const stored = JSON.parse(raw) as StoredOutputOffset;
    return stored.startedAt === startedAt ? stored.byteOffset : 0;
  } catch {
    // Private browsing, disabled storage, or a malformed stored value -
    // resuming from 0 (a full replay) is a safe fallback, not a crash.
    return 0;
  }
}

function writeStoredOffset(startedAt: string, byteOffset: number): void {
  try {
    const stored: StoredOutputOffset = { startedAt, byteOffset };
    window.sessionStorage.setItem(OUTPUT_OFFSET_STORAGE_KEY, JSON.stringify(stored));
  } catch {
    // If storage isn't available, a reload just can't resume - it still
    // reconnects and shows new output going forward.
  }
}

function clearStoredOffset(): void {
  try {
    window.sessionStorage.removeItem(OUTPUT_OFFSET_STORAGE_KEY);
  } catch {
    // Nothing to clean up if storage was never writable.
  }
}

/** Turns any error a fetch/SSE call can throw into operator-facing text.
 * `Event` covers EventSource's connection-level errors (a real DOM Event,
 * not an Error) - see OutputHandlers.onError in api/client.ts. */
function describeError(error: unknown): string {
  if (error instanceof Event) {
    return "Lost the connection to the server.";
  }
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

/** Returns `themes` with the batch matching `preview` updated to that preview's
 * fresh per-batch counts, so the persistent dropdown stays in step with a
 * re-check (which otherwise leaves the once-fetched all-mode themes doc stale).
 * A per-batch validate doc's top-level ready_to_upload/counts are scoped to that
 * batch (see ia_bulk.validate_json), matching an all-mode batches[] entry.
 * Returns the same reference when nothing matches, to skip a needless render. */
function withBatchFromPreview(themes: ValidateDoc, preview: ValidateDoc): ValidateDoc {
  if (preview.batch === null || themes.batches === null) return themes;
  const batchValue = preview.batch;
  let matched = false;
  const batches = themes.batches.map((batch) => {
    if (batch.value !== batchValue) return batch;
    matched = true;
    return { ...batch, ready_to_upload: preview.ready_to_upload, counts: preview.counts };
  });
  return matched ? { ...themes, batches } : themes;
}

/** The persistent theme picker shows in every state except the ones with
 * nothing to pick from: still booting, another run holds the lock, or a
 * terminal error. */
function themePickerVisible(state: AppState): boolean {
  return state.kind !== "loading" && state.kind !== "terminal-run" && state.kind !== "error";
}

/** The theme the dropdown shows as selected - the batch of whatever the
 * current state is working on, or undefined when nothing is picked yet. */
function currentBatch(state: AppState): string | undefined {
  switch (state.kind) {
    case "checking":
    case "previewed":
    case "confirming":
    case "running":
    case "stopping":
      return state.batch;
    default:
      return undefined;
  }
}

function announcementFor(state: AppState, starting: boolean): string {
  switch (state.kind) {
    case "loading":
      return "Loading";
    case "choosing":
      return "Choose a theme";
    case "checking":
      return "Checking preview";
    case "previewed":
      return "Preview ready";
    case "confirming":
      return starting ? "Starting upload" : "Preview ready";
    case "running":
      return "Upload running";
    case "stopping":
      return "Stopping the upload";
    case "finished":
      return "Upload finished";
    case "terminal-run":
      return "Another run is already in progress";
    case "error":
      return `Error: ${state.message}`;
    default: {
      const exhaustiveCheck: never = state;
      return exhaustiveCheck;
    }
  }
}

function TerminalRunView({ holder }: { holder: TerminalRunHolder }) {
  return (
    <section className="rounded border border-border bg-surface p-4 text-text">
      <p className="font-semibold">Another run is already in progress on this machine.</p>
      {holder ? (
        <p className="mt-2 text-muted">
          {`Started ${holder.started_at} for "${holder.project}"${holder.batch ? ` (${holder.batch})` : ""}.`}
        </p>
      ) : (
        <p className="mt-2 text-muted">Its details are not available from here.</p>
      )}
    </section>
  );
}

export default function App() {
  const [state, dispatch] = useReducer(reducer, { kind: "loading" });
  const [identity, setIdentity] = useState<PageIdentity | null>(null);
  const [lines, setLines] = useState<string[]>([]);
  // The theme list for the persistent picker. App-level (not in the reducer)
  // so the dropdown is populated in every state, not only "choosing". Fetched
  // once identity is known and refreshed after a run finishes.
  const [themes, setThemes] = useState<ValidateDoc | null>(null);
  const eventSourceRef = useRef<ReturnType<typeof openOutput> | null>(null);
  const rememberedBundleStampRef = useRef<string | null>(null);
  // Total lines ever appended this session. `lines[0]`'s absolute sequence
  // number is this minus the buffer length, which RunningOutput uses as a
  // stable React key base so a capped buffer dropping lines off the front does
  // not force every remaining line to re-render.
  const linesSeenRef = useRef(0);
  // True while startRun is in flight - see handleConfirmStart.
  const [starting, setStarting] = useState(false);
  // The theme currently uploading, remembered so the Finished screen can fetch
  // that theme's rows after the run and show the same per-row breakdown the
  // Preview does. `finishedThemeRows` holds those fetched rows (null = omit).
  const runningBatchRef = useRef<string | null>(null);
  const [finishedThemeRows, setFinishedThemeRows] = useState<ValidateRow[] | null>(null);

  // Mount only - fetch status once and route to the matching screen. Choose-
  // another does NOT come back through here: it goes straight from
  // "finished" to "choosing" (see reducer.ts's fromFinished) so it never
  // re-fetches this stateless status and loops back to "finished" again.
  useEffect(() => {
    if (state.kind !== "loading") return;
    let cancelled = false;
    getStatus()
      .then((status) => {
        if (cancelled) return;
        setIdentity({ project: status.project, collection: status.collection, live: status.live });
        dispatch({ type: "status/received", run: status.run });
      })
      .catch((error: unknown) => {
        if (!cancelled) dispatch({ type: "error", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  // Load the theme list once identity is known, and again whenever `themes`
  // is cleared to null - initially, and after a run finishes (see below).
  useEffect(() => {
    if (identity === null || themes !== null) return;
    let cancelled = false;
    getThemes()
      .then((loaded) => {
        if (!cancelled) setThemes(loaded);
      })
      .catch((error: unknown) => {
        if (!cancelled) dispatch({ type: "error", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [identity, themes]);

  // When a run this page was watching finishes, its theme's readiness has
  // changed - drop the cached list to trigger the refetch above. Tracked via
  // the previous kind so a fresh mount that lands straight on "finished" (a
  // run that ended before load) doesn't refetch what mount already fetched.
  const previousKindRef = useRef(state.kind);
  useEffect(() => {
    const wasRunning = previousKindRef.current === "running" || previousKindRef.current === "stopping";
    previousKindRef.current = state.kind;
    if (state.kind === "finished" && wasRunning) setThemes(null);
  }, [state.kind]);

  // Remember which theme is uploading, and once its run finishes fetch that
  // theme's rows so the Finished screen can show the same per-row breakdown
  // the Preview does (the run recorded its new uploads to the Sheet).
  useEffect(() => {
    if (state.kind === "running" || state.kind === "stopping") {
      runningBatchRef.current = state.batch;
      return;
    }
    if (state.kind !== "finished") {
      setFinishedThemeRows(null);
      return;
    }
    const batch = runningBatchRef.current;
    if (batch === null) return;
    let cancelled = false;
    getPreview(batch)
      .then((preview) => {
        if (!cancelled) setFinishedThemeRows(preview.rows ?? []);
      })
      .catch(() => {
        // A refused re-fetch just means no breakdown here; the summary still shows.
        if (!cancelled) setFinishedThemeRows(null);
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  // Covers both a theme selection and Re-check - both land on "checking".
  useEffect(() => {
    if (state.kind !== "checking") return;
    let cancelled = false;
    getPreview(state.batch)
      .then((preview) => {
        if (cancelled) return;
        dispatch({ type: "preview/loaded", preview, checkedAt: new Date().toISOString() });
        // Keep the persistent dropdown in step with this check: re-checking a
        // theme (e.g. after fixing a row) changes its ready/not-ready counts.
        setThemes((current) => (current ? withBatchFromPreview(current, preview) : current));
      })
      .catch((error: unknown) => {
        if (!cancelled) dispatch({ type: "preview/failed", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  // Close the output stream once a run is no longer active, and on unmount.
  useEffect(() => {
    if (state.kind !== "running" && state.kind !== "stopping") {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
    }
  }, [state]);
  useEffect(() => {
    return () => eventSourceRef.current?.close();
  }, []);

  // (Re)subscribe on every entry into "running"/"stopping", not just a
  // fresh Confirm - covers a mid-run mount/reload/second tab too.
  useEffect(() => {
    if (state.kind !== "running" && state.kind !== "stopping") return;
    if (eventSourceRef.current !== null) return;
    ensureSubscribed(state.startedAt, readStoredOffset(state.startedAt));
  }, [state]);

  // Poll /api/health and hard-reload once bundle_stamp changes (a deploy).
  useEffect(() => {
    let cancelled = false;
    async function checkHealth() {
      try {
        const health = await getHealth();
        if (cancelled) return;
        if (rememberedBundleStampRef.current === null) {
          rememberedBundleStampRef.current = health.bundle_stamp;
        } else if (health.bundle_stamp !== rememberedBundleStampRef.current) {
          window.location.reload();
        }
      } catch {
        // A transient health-check failure isn't worth surfacing as an
        // app-level error - the next poll tries again.
      }
    }
    void checkHealth();
    const intervalId = window.setInterval(checkHealth, HEALTH_POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, []);

  function handleThemeSelect(batch: string) {
    // Picking a theme abandons any prior run's console output and stored
    // offset - the cleanup Choose-another used to do, now that the persistent
    // dropdown is how you move on to the next theme.
    setLines([]);
    clearStoredOffset();
    dispatch({ type: "theme/selected", batch });
  }

  function handleRecheck() {
    dispatch({ type: "recheck/clicked" });
  }

  // The single place an EventSource is ever opened; guarded so a run
  // already streaming is never given a second connection.
  function ensureSubscribed(startedAt: string, fromOffset: number) {
    if (eventSourceRef.current !== null) return;
    const handlers: OutputHandlers = {
      onLine: (text, byteOffset) => {
        writeStoredOffset(startedAt, byteOffset);
        linesSeenRef.current += 1;
        setLines((previous) => appendLineCapped(previous, text));
      },
      onProgress: (progress) => dispatch({ type: "sse/progress", ...progress }),
      onFinished: (ending) => {
        dispatch({ type: "sse/finished", ending });
        eventSourceRef.current?.close();
        eventSourceRef.current = null;
        clearStoredOffset();
        setLines([]);
      },
      onError: (error) => dispatch({ type: "error", message: describeError(error) }),
    };
    eventSourceRef.current = openOutput(handlers, fromOffset);
  }

  // Both handlers dispatch "start/clicked" first since the reducer only
  // accepts confirm/yes|cancel from "confirming" - the Radix dialog itself
  // is the "confirming" UI (see StartDialog.tsx). confirm/yes waits for
  // startRun to resolve, so the output stream is never opened before the
  // run exists server-side; `starting` blanks out Cancel/Confirm for that
  // window so the run can't be orphaned by a Cancel that arrives mid-flight.
  function handleConfirmStart(batch: string) {
    dispatch({ type: "start/clicked" });
    setStarting(true);
    setLines([]);
    clearStoredOffset();
    startRun(batch)
      .then((response) => {
        setStarting(false);
        dispatch({ type: "confirm/yes", startedAt: response.started_at });
      })
      .catch((error: unknown) => {
        setStarting(false);
        dispatch({ type: "error", message: describeError(error) });
      });
  }

  function handleCancelStart() {
    dispatch({ type: "start/clicked" });
    dispatch({ type: "confirm/cancel" });
  }

  function handleStop() {
    dispatch({ type: "stop/clicked" });
    stopRun().catch((error: unknown) => dispatch({ type: "error", message: describeError(error) }));
  }


  function renderBody() {
    switch (state.kind) {
      case "loading":
        return <p className="text-muted">Loading…</p>;

      case "choosing":
        return themes === null ? (
          <p className="text-muted">Loading themes…</p>
        ) : (
          <p className="text-muted">Pick a theme above to get started.</p>
        );

      case "checking":
        return <p className="text-muted">Checking…</p>;

      case "previewed":
      case "confirming":
        // While starting, no Cancel/Confirm is rendered at all - closes
        // the window for a Cancel to orphan a run startRun already began.
        return starting ? (
          <p className="text-muted" role="status">
            Starting upload…
          </p>
        ) : (
          <>
            <Preview doc={state.preview} checkedAt={state.checkedAt} onRecheck={handleRecheck} />
            {state.preview.ready_to_upload > 0 && (
              <div className="mt-4">
                <StartDialog
                  count={state.preview.ready_to_upload}
                  batch={state.batch}
                  live={identity?.live ?? false}
                  onConfirm={() => handleConfirmStart(state.batch)}
                  onCancel={handleCancelStart}
                />
              </div>
            )}
          </>
        );

      case "running":
      case "stopping":
        return (
          <RunningOutput
            lines={lines}
            keyBase={linesSeenRef.current - lines.length}
            done={state.done}
            planned={state.planned}
            current={state.current}
            stopping={state.kind === "stopping"}
            onStop={handleStop}
          />
        );

      case "finished":
        return <Finished ending={state.ending} themeRows={finishedThemeRows} />;

      case "terminal-run":
        return <TerminalRunView holder={state.holder} />;

      case "error":
        return (
          <section role="alert" className="rounded border border-danger-border bg-danger-bg p-4 text-danger">
            {state.message}
          </section>
        );

      default: {
        const exhaustiveCheck: never = state;
        return exhaustiveCheck;
      }
    }
  }

  return (
    <main className="min-h-screen bg-bg p-4">
      <div className="mx-auto w-full max-w-[1000px]">
        {identity && (
          <Header project={identity.project} collection={identity.collection} live={identity.live}>
            {themePickerVisible(state) && (
              <ThemePicker
                batches={themes?.batches ?? []}
                value={currentBatch(state)}
                disabled={themes === null || starting || state.kind === "running" || state.kind === "stopping"}
                loading={themes === null}
                onSelect={handleThemeSelect}
              />
            )}
          </Header>
        )}
        <div className="mt-4">{renderBody()}</div>
        <LiveRegion message={announcementFor(state, starting)} />
      </div>
    </main>
  );
}
