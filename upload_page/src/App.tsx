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

function appendLineCapped(previous: string[], line: string): string[] {
  const next = previous.length < MAX_BUFFERED_LINES ? [...previous, line] : [...previous.slice(1), line];
  return next;
}

interface PageIdentity {
  project: string;
  collection: string;
  live: boolean;
}

// Resuming the output stream across a reload (see the resubscribe effect
// below) needs to remember, per run, the last byte offset already shown -
// sessionStorage survives a reload but not a closed tab, which matches a
// run's own lifetime. Keyed by batch so a stale offset from a finished run
// is never mistaken for the current one's.
const OUTPUT_OFFSET_STORAGE_KEY = "upload-page:output-offset";

interface StoredOutputOffset {
  batch: string;
  byteOffset: number;
}

function readStoredOffset(batch: string): number {
  try {
    const raw = window.sessionStorage.getItem(OUTPUT_OFFSET_STORAGE_KEY);
    if (!raw) return 0;
    const stored = JSON.parse(raw) as StoredOutputOffset;
    return stored.batch === batch ? stored.byteOffset : 0;
  } catch {
    // Private browsing, disabled storage, or a malformed stored value -
    // resuming from 0 (a full replay) is a safe fallback, not a crash.
    return 0;
  }
}

function writeStoredOffset(batch: string, byteOffset: number): void {
  try {
    const stored: StoredOutputOffset = { batch, byteOffset };
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

function announcementFor(state: AppState): string {
  switch (state.kind) {
    case "loading":
      return "Loading";
    case "choosing":
      return state.themes === null ? "Loading themes" : "Choose a theme";
    case "checking":
      return "Checking preview";
    case "previewed":
      return "Preview ready";
    case "confirming":
      return "Preview ready";
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
  const eventSourceRef = useRef<ReturnType<typeof openOutput> | null>(null);
  const rememberedBundleStampRef = useRef<string | null>(null);
  // Guards against a double Confirm click re-entering handleConfirmStart
  // while its startRun call is still in flight (state sits in "confirming"
  // for that whole window now - see handleConfirmStart below).
  const startInFlightRef = useRef(false);

  // Mount, and every return trip through "loading" (Choose-another routes
  // back here too - see the choose-another handler below) - fetch status
  // and route to the matching screen. The reducer returns the *same*
  // AppState reference for a no-op action, so this only re-fires when we
  // are genuinely (re)entering "loading", not on every unrelated render.
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

  // Load the theme list the first time the picker is shown (including
  // after Choose-another routes back through "loading" -> "choosing").
  useEffect(() => {
    if (state.kind !== "choosing" || state.themes !== null) return;
    let cancelled = false;
    getThemes()
      .then((themes) => {
        if (!cancelled) dispatch({ type: "themes/loaded", themes });
      })
      .catch((error: unknown) => {
        if (!cancelled) dispatch({ type: "error", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  // "checking" is entered both by picking a theme and by Re-check - one
  // effect covers both, since both just mean "fetch a fresh preview for
  // this batch".
  useEffect(() => {
    if (state.kind !== "checking") return;
    let cancelled = false;
    getPreview(state.batch)
      .then((preview) => {
        if (!cancelled) {
          dispatch({ type: "preview/loaded", preview, checkedAt: new Date().toISOString() });
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) dispatch({ type: "preview/failed", message: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [state]);

  // Close the output stream once a run is no longer active, and on
  // unmount - never leave a dangling EventSource open. This only ever
  // closes; it can never fire in the same render as the resubscribe
  // effect below, since that one only opens while state IS running or
  // stopping, and this one only closes when it is neither.
  useEffect(() => {
    if (state.kind !== "running" && state.kind !== "stopping") {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
    }
  }, [state]);
  useEffect(() => {
    return () => eventSourceRef.current?.close();
  }, []);

  // (Re)subscribe to the output stream for every path that lands on
  // "running"/"stopping" - not just a fresh Confirm click. A page reload
  // mid-run (including one this task's own health-reload effect triggers
  // mid-upload), a second tab, or simply mounting while a run is already
  // active (getStatus() -> page_run_active) all route straight into
  // "running" with no EventSource of their own - without this effect,
  // RunningOutput would render frozen (no lines, stale done/planned) and
  // Stop would appear to do nothing. Guarded on eventSourceRef.current
  // being null so a run already streaming is never resubscribed, and
  // resumes from the last byte offset this browser saw for this batch
  // (0 for a run this tab has never seen output from).
  useEffect(() => {
    if (state.kind !== "running" && state.kind !== "stopping") return;
    if (eventSourceRef.current !== null) return;
    ensureSubscribed(state.batch, readStoredOffset(state.batch));
  }, [state]);

  // Poll /api/health for a changed bundle_stamp - a new deploy - and hard
  // reload when it changes. Independent of AppState: a stale bundle needs
  // reloading no matter what screen is showing.
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
    dispatch({ type: "theme/selected", batch });
  }

  function handleRecheck() {
    dispatch({ type: "recheck/clicked" });
  }

  // The single place an EventSource is ever opened - called from the
  // resubscribe effect above for every entry into "running"/"stopping".
  // Guarded so a run already streaming is never given a second connection.
  function ensureSubscribed(batch: string, fromOffset: number) {
    if (eventSourceRef.current !== null) return;
    const handlers: OutputHandlers = {
      onLine: (text, byteOffset) => {
        writeStoredOffset(batch, byteOffset);
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

  // StartDialog's own Trigger opens/closes the confirmation UI entirely on
  // its own (it is an uncontrolled Radix dialog - see StartDialog.tsx) -
  // there is no moment for this component to observe separately from
  // Confirm/Cancel themselves firing, so both handlers dispatch
  // "start/clicked" first: the reducer only accepts confirm/yes and
  // confirm/cancel from "confirming", never from "previewed" directly (see
  // reducer.test.ts's impossible-transition coverage). The Radix dialog IS
  // the "confirming" screen - it renders identically to "previewed" (see
  // renderBody below) - so the reducer's confirming state, whether it's
  // painted for one tick or the width of a network call, is never a
  // visually distinct screen of its own.
  //
  // Unlike Cancel, Confirm must NOT dispatch confirm/yes in the same tick:
  // the resubscribe effect opens the output stream the instant state
  // becomes "running", so doing that before startRun's POST has actually
  // created the run server-side raced the server's own "current run"
  // lookup - landing on nothing (a 204) or, worse, the *previous* finished
  // run's output. So confirm/yes is deferred until startRun resolves,
  // which also means "confirming" can now genuinely be on screen for the
  // length of that request - startInFlightRef guards against a second
  // Confirm click (the trigger button is technically clickable again once
  // Radix's own dialog closes) re-entering this function mid-flight.
  function handleConfirmStart(batch: string) {
    if (startInFlightRef.current) return;
    startInFlightRef.current = true;
    dispatch({ type: "start/clicked" });
    setLines([]);
    clearStoredOffset();
    startRun(batch)
      .then(() => {
        startInFlightRef.current = false;
        // Only now does a run actually exist server-side for the
        // resubscribe effect to attach to.
        dispatch({ type: "confirm/yes" });
      })
      .catch((error: unknown) => {
        startInFlightRef.current = false;
        // Stay out of "running" - e.g. a 409 because a run was already
        // started elsewhere. Surfacing this as an error (rather than
        // silently reopening the dialog) matches how every other fetch
        // failure on this page is handled.
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

  function handleChooseAnother() {
    setLines([]);
    clearStoredOffset();
    dispatch({ type: "choose-another/clicked" });
  }

  function renderBody() {
    switch (state.kind) {
      case "loading":
        return <p className="text-muted">Loading…</p>;

      case "choosing":
        return state.themes === null ? (
          <p className="text-muted">Loading themes…</p>
        ) : (
          <ThemePicker batches={state.themes.batches ?? []} onSelect={handleThemeSelect} />
        );

      case "checking":
        return <p className="text-muted">Checking…</p>;

      case "previewed":
      case "confirming":
        return (
          <>
            <Preview doc={state.preview} checkedAt={state.checkedAt} onRecheck={handleRecheck} />
            <div className="mt-4">
              <StartDialog
                count={state.preview.ready_to_upload}
                batch={state.batch}
                live={identity?.live ?? false}
                onConfirm={() => handleConfirmStart(state.batch)}
                onCancel={handleCancelStart}
              />
            </div>
          </>
        );

      case "running":
      case "stopping":
        return (
          <RunningOutput
            lines={lines}
            done={state.done}
            planned={state.planned}
            stopping={state.kind === "stopping"}
            onStop={handleStop}
          />
        );

      case "finished":
        return <Finished ending={state.ending} onChooseAnother={handleChooseAnother} />;

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
      {identity && <Header project={identity.project} collection={identity.collection} live={identity.live} />}
      <div className="mt-4">{renderBody()}</div>
      <LiveRegion message={announcementFor(state)} />
    </main>
  );
}
