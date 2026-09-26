// The upload page's app state machine: what screen is showing and the
// data it carries, plus every event that can move it from one screen to
// another. `reducer.ts` is the only thing allowed to interpret these -
// components (Tasks 13/14) dispatch Actions and render off AppState, and
// never construct a next AppState by hand.
//
// AppState and Action are both discriminated unions (`kind` / `type`
// respectively) so a `switch` over either narrows exhaustively - see
// reducer.ts's `never` checks.

import type { Ending, RunState, SseProgressEvent, ValidateDoc } from "../api/schemas";

/** The `holder` a `terminal_run_active` RunState carries - someone else
 * (a terminal `ia_bulk.py upload` run, not this page) holds the run lock.
 * `null` means the lock is held but the holder's identity is unknown. */
export type TerminalRunHolder = Extract<RunState, { kind: "terminal_run_active" }>["holder"];

// ---------------------------------------------------------------------
// AppState
// ---------------------------------------------------------------------

/** Before the first `GET /api/status` response has come back. */
export interface LoadingState {
  kind: "loading";
}

/** The theme picker. `themes` is null until `GET /api/themes` resolves;
 * the picker can render its loading state without leaving `choosing`. */
export interface ChoosingState {
  kind: "choosing";
  themes: ValidateDoc | null;
}

/** A theme was picked; its preview (`GET /api/preview`) is in flight. */
export interface CheckingState {
  kind: "checking";
  batch: string;
}

/** The preview loaded successfully and is on screen, awaiting Start or
 * Re-check. `checkedAt` is when this preview was fetched, for display. */
export interface PreviewedState {
  kind: "previewed";
  batch: string;
  preview: ValidateDoc;
  checkedAt: string;
}

/** The Start confirmation dialog is open over the previewed screen. */
export interface ConfirmingState {
  kind: "confirming";
  batch: string;
  preview: ValidateDoc;
  checkedAt: string;
}

/** A run is in progress. `planned` is null until the server reports a
 * total (mirrors RunState's `page_run_active.planned`). */
export interface RunningState {
  kind: "running";
  batch: string;
  done: number;
  planned: number | null;
}

/** Stop was requested; the run is winding down but hasn't finished yet. */
export interface StoppingState {
  kind: "stopping";
  batch: string;
  done: number;
  planned: number | null;
}

/** The run ended, one way or another - see Ending's five kinds. */
export interface FinishedState {
  kind: "finished";
  ending: Ending;
}

/** Someone else holds the run lock (a terminal run, not this page's own).
 * There is nothing for this page to do but show who and wait. */
export interface TerminalRunState {
  kind: "terminal-run";
  holder: TerminalRunHolder;
}

/** An unrecoverable problem: a preview refusal, a boundary parse failure,
 * a network error - anything reported via the `error` action. */
export interface ErrorState {
  kind: "error";
  message: string;
}

export type AppState =
  | LoadingState
  | ChoosingState
  | CheckingState
  | PreviewedState
  | ConfirmingState
  | RunningState
  | StoppingState
  | FinishedState
  | TerminalRunState
  | ErrorState;

// ---------------------------------------------------------------------
// Action
// ---------------------------------------------------------------------

/**
 * The initial `GET /api/status` result. Only `loading` interprets this -
 * it is how the app decides which screen to start on by routing on
 * `run.kind`. From every other state it is ignored (see reducer.ts).
 */
export interface StatusReceivedAction {
  type: "status/received";
  run: RunState;
}

export interface ThemesLoadedAction {
  type: "themes/loaded";
  themes: ValidateDoc;
}

export interface ThemeSelectedAction {
  type: "theme/selected";
  batch: string;
}

export interface PreviewLoadedAction {
  type: "preview/loaded";
  preview: ValidateDoc;
  checkedAt: string;
}

/** The server refused to produce a preview (e.g. an invalid batch). */
export interface PreviewFailedAction {
  type: "preview/failed";
  message: string;
}

export interface RecheckClickedAction {
  type: "recheck/clicked";
}

export interface StartClickedAction {
  type: "start/clicked";
}

export interface ConfirmCancelAction {
  type: "confirm/cancel";
}

export interface ConfirmYesAction {
  type: "confirm/yes";
}

/** Wraps the parsed SSE `progress` event as-is - same done/planned shape
 * the server sends, so there is nothing for the App to translate. */
export type SseProgressAction = { type: "sse/progress" } & SseProgressEvent;

export interface StopClickedAction {
  type: "stop/clicked";
}

/** The parsed SSE `finished` event's Ending (the event itself is
 * `{ending: Ending}` - see schemas.ts's SseFinishedEvent). */
export interface SseFinishedAction {
  type: "sse/finished";
  ending: Ending;
}

export interface ChooseAnotherClickedAction {
  type: "choose-another/clicked";
}

/** A terminal, unrecoverable problem - valid from any state. */
export interface ErrorAction {
  type: "error";
  message: string;
}

export type Action =
  | StatusReceivedAction
  | ThemesLoadedAction
  | ThemeSelectedAction
  | PreviewLoadedAction
  | PreviewFailedAction
  | RecheckClickedAction
  | StartClickedAction
  | ConfirmCancelAction
  | ConfirmYesAction
  | SseProgressAction
  | StopClickedAction
  | SseFinishedAction
  | ChooseAnotherClickedAction
  | ErrorAction;
