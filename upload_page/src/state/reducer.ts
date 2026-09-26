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
  ChoosingState,
  CheckingState,
  ConfirmingState,
  FinishedState,
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
      return { kind: "choosing", themes: null };
    case "terminal_run_active":
      return { kind: "terminal-run", holder: run.holder };
    case "page_run_active":
      return { kind: "running", batch: run.batch, done: run.done, planned: run.planned, current: run.current };
    case "finished":
      return { kind: "finished", ending: run.ending };
    default: {
      const exhaustiveCheck: never = run;
      return exhaustiveCheck;
    }
  }
}

function fromChoosing(state: ChoosingState, action: Action): AppState {
  switch (action.type) {
    case "themes/loaded":
      return { kind: "choosing", themes: action.themes };
    case "theme/selected":
      return { kind: "checking", batch: action.batch };
    default:
      return state;
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
      return { kind: "running", batch: state.batch, done: 0, planned: state.preview.ready_to_upload, current: null };
    default:
      return state;
  }
}

function fromRunning(state: RunningState, action: Action): AppState {
  switch (action.type) {
    case "sse/progress":
      return { kind: "running", batch: state.batch, done: action.done, planned: action.planned, current: action.current };
    case "stop/clicked":
      return { kind: "stopping", batch: state.batch, done: state.done, planned: state.planned, current: state.current };
    case "sse/finished":
      return { kind: "finished", ending: action.ending };
    default:
      return state;
  }
}

function fromStopping(state: StoppingState, action: Action): AppState {
  switch (action.type) {
    case "sse/progress":
      return { kind: "stopping", batch: state.batch, done: action.done, planned: action.planned, current: action.current };
    case "sse/finished":
      return { kind: "finished", ending: action.ending };
    default:
      return state;
  }
}

function fromFinished(state: FinishedState, action: Action): AppState {
  switch (action.type) {
    // Straight to "choosing", not "loading" - "loading" re-fetches
    // /api/status, which would just report "finished" again (it's derived
    // from the newest page-run folder) and bounce right back here.
    case "choose-another/clicked":
      return { kind: "choosing", themes: null };
    default:
      return state;
  }
}

export function reducer(state: AppState, action: Action): AppState {
  // Accepted from every state, including "error" itself.
  if (action.type === "error") {
    return { kind: "error", message: action.message };
  }

  switch (state.kind) {
    case "loading":
      return fromLoading(state, action);
    case "choosing":
      return fromChoosing(state, action);
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
    case "finished":
      return fromFinished(state, action);
    // "terminal-run" and "error" have no outgoing transitions besides the
    // "error" action already handled above - every other action is a
    // no-op from here.
    case "terminal-run":
    case "error":
      return state;
    default: {
      const exhaustiveCheck: never = state;
      return exhaustiveCheck;
    }
  }
}
