// The upload page's state machine transitions. Pure by design: no fetch,
// no timers, no DOM - just (AppState, Action) -> AppState. The App (Task
// 14) is the only thing that performs I/O; it dispatches the results
// (a parsed response, an SSE event, a click) in as Actions.
//
// Every (state, action) pair not listed below is an impossible event and
// leaves the state unchanged - deliberately returning the *same* object
// reference, not a copy, so components relying on referential equality
// (e.g. to skip a re-render) see nothing happened.

import type {
  Action,
  AppState,
  CheckingState,
  ConfirmingState,
  LoadingState,
  PreviewedState,
  RunningState,
  StoppingState,
} from "./types";
import type { RunState } from "../api/schemas";

function fromLoading(state: LoadingState, action: Action): AppState {
  if (action.type !== "status/received") return state;

  const run: RunState = action.run;
  switch (run.kind) {
    case "idle":
      return { kind: "choosing" };
    case "terminal_run_active":
      return { kind: "terminal-run", holder: run.holder };
    case "page_run_active":
      return { kind: "running", batch: run.batch, startedAt: run.started_at, done: run.done, planned: run.planned, current: run.current };
    case "finished":
      // A finished run reported at page load is a stale leftover of the newest
      // page-run folder (status is stateless), not this session's own work, and
      // showing its summary confused operators ("why does it say 4 uploaded?").
      // Start at the picker instead; a run started this session still shows its
      // result via the SSE "finished" event (fromRunning/fromStopping).
      return { kind: "choosing" };
    default: {
      const exhaustiveCheck: never = run;
      return exhaustiveCheck;
    }
  }
}

function fromChecking(state: CheckingState, action: Action): AppState {
  switch (action.type) {
    case "preview/loaded":
      return { kind: "previewed", batch: state.batch, preview: action.preview, checkedAt: action.checkedAt };
    case "preview/failed":
      return { kind: "error", message: action.message };
    default:
      return state;
  }
}

function fromPreviewed(state: PreviewedState, action: Action): AppState {
  switch (action.type) {
    case "recheck/clicked":
      return { kind: "checking", batch: state.batch };
    case "start/clicked":
      return { kind: "confirming", batch: state.batch, preview: state.preview, checkedAt: state.checkedAt };
    default:
      return state;
  }
}

function fromConfirming(state: ConfirmingState, action: Action): AppState {
  switch (action.type) {
    case "confirm/cancel":
      return { kind: "previewed", batch: state.batch, preview: state.preview, checkedAt: state.checkedAt };
    case "confirm/yes":
      return { kind: "running", batch: state.batch, startedAt: action.startedAt, done: 0, planned: state.preview.ready_to_upload, current: null };
    default:
      return state;
  }
}

function fromRunning(state: RunningState, action: Action): AppState {
  switch (action.type) {
    case "sse/progress":
      return { kind: "running", batch: state.batch, startedAt: state.startedAt, done: action.done, planned: action.planned, current: action.current };
    case "stop/clicked":
      return { kind: "stopping", batch: state.batch, startedAt: state.startedAt, done: state.done, planned: state.planned, current: state.current };
    case "sse/finished":
      return { kind: "finished", ending: action.ending };
    default:
      return state;
  }
}

function fromStopping(state: StoppingState, action: Action): AppState {
  switch (action.type) {
    case "sse/progress":
      return { kind: "stopping", batch: state.batch, startedAt: state.startedAt, done: action.done, planned: action.planned, current: action.current };
    case "sse/finished":
      return { kind: "finished", ending: action.ending };
    default:
      return state;
  }
}

export function reducer(state: AppState, action: Action): AppState {
  // Accepted from every state, including "error" itself.
  if (action.type === "error") {
    return { kind: "error", message: action.message };
  }

  // Picking a theme from the persistent dropdown (re)starts its readiness
  // check. Enabled wherever the dropdown is enabled; a no-op where it is
  // absent or disabled - loading, an active run (running/stopping),
  // terminal-run, error.
  if (action.type === "theme/selected") {
    const pickerEnabled =
      state.kind === "choosing" ||
      state.kind === "checking" ||
      state.kind === "previewed" ||
      state.kind === "confirming" ||
      state.kind === "finished";
    return pickerEnabled ? { kind: "checking", batch: action.batch } : state;
  }

  switch (state.kind) {
    case "loading":
      return fromLoading(state, action);
    case "checking":
      return fromChecking(state, action);
    case "previewed":
      return fromPreviewed(state, action);
    case "confirming":
      return fromConfirming(state, action);
    case "running":
      return fromRunning(state, action);
    case "stopping":
      return fromStopping(state, action);
    // No transitions of their own: "choosing" and "finished" advance only via
    // the global theme/selected above (the persistent picker), and
    // "terminal-run"/"error" via the global error already handled above.
    case "choosing":
    case "finished":
    case "terminal-run":
    case "error":
      return state;
    default: {
      const exhaustiveCheck: never = state;
      return exhaustiveCheck;
    }
  }
}
