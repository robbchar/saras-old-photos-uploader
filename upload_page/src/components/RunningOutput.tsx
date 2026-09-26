// The live console output pane for a run in progress. App owns the line
// buffer and the SSE subscription that fills it; this component only knows
// how to render what it is given and ask the operator to confirm a stop.
//
// Auto-scroll follows the classic "stick to bottom" chat/log convention:
// while the reader is at (or near) the bottom, new lines keep it pinned
// there; the moment they scroll up to read back, new lines stop yanking
// the view away from them. Restoring that with plain scrollTop math is
// simpler and more testable here than a scroll-anchoring library.

import { useEffect, useRef, useState } from "react";

export interface RunningOutputProps {
  lines: string[];
  done: number;
  planned: number | null;
  stopping: boolean;
  onStop: () => void;
}

/** How close to the bottom (in pixels) still counts as "at the bottom" -
 * a reader a few pixels short of the very end is still reading live. */
const NEAR_BOTTOM_THRESHOLD_PX = 24;

/** A bare `\r` inside a logical line means "rewrite this line" (a
 * progress-bar-style update) - only the text after the last `\r` is what
 * is actually on screen. `\r\n` was already folded into a plain newline
 * server-side, so it never reaches here as a line-internal character. */
function visibleText(line: string): string {
  const lastCarriageReturn = line.lastIndexOf("\r");
  return lastCarriageReturn === -1 ? line : line.slice(lastCarriageReturn + 1);
}

function progressLabel(done: number, planned: number | null): string {
  return planned === null ? `${done} so far` : `${done} of ${planned}`;
}

function isNearBottom(element: HTMLElement): boolean {
  const distanceFromBottom = element.scrollHeight - element.scrollTop - element.clientHeight;
  return distanceFromBottom <= NEAR_BOTTOM_THRESHOLD_PX;
}

export function RunningOutput({ lines, done, planned, stopping, onStop }: RunningOutputProps) {
  const [confirmingStop, setConfirmingStop] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);
  // Whether the reader was at the bottom just before this update - a ref,
  // not state, since tracking it must never itself trigger a re-render.
  const wasNearBottomRef = useRef(true);

  useEffect(() => {
    const element = logRef.current;
    if (element && wasNearBottomRef.current) {
      element.scrollTop = element.scrollHeight;
    }
  }, [lines]);

  function handleScroll(event: React.UIEvent<HTMLDivElement>) {
    wasNearBottomRef.current = isNearBottom(event.currentTarget);
  }

  function handleStopClick() {
    setConfirmingStop(true);
  }

  function handleConfirmStop() {
    setConfirmingStop(false);
    onStop();
  }

  function handleCancelStop() {
    setConfirmingStop(false);
  }

  return (
    <section className="rounded border border-border bg-surface p-4 text-text">
      <header className="flex items-center justify-between gap-4 font-sans">
        <span className="text-muted">{progressLabel(done, planned)}</span>
        {stopping ? (
          <button
            type="button"
            disabled
            className="rounded border border-border-strong px-3 py-1.5 text-faint"
          >
            Stopping after the current photo…
          </button>
        ) : confirmingStop ? (
          // A plain labeled group, not a modal - this is an inline
          // two-button confirm with no focus trap or Escape handling, so
          // "alertdialog" would overstate its semantics to assistive tech.
          <span role="group" aria-label="Confirm stop" className="flex items-center gap-2">
            <span className="text-text">Stop after the current photo?</span>
            <button
              type="button"
              onClick={handleConfirmStop}
              className="rounded border border-border-strong px-3 py-1.5 text-text"
            >
              Stop
            </button>
            <button
              type="button"
              onClick={handleCancelStop}
              className="rounded border border-border-strong px-3 py-1.5 text-text"
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            onClick={handleStopClick}
            className="rounded border border-border-strong px-3 py-1.5 text-text"
          >
            Stop
          </button>
        )}
      </header>

      <div
        ref={logRef}
        onScroll={handleScroll}
        role="log"
        aria-label="Run output"
        className="mt-4 h-80 overflow-y-auto rounded border border-border bg-bg p-2 font-mono text-sm text-text"
      >
        {lines.map((line, index) => (
          <div key={index}>{visibleText(line)}</div>
        ))}
      </div>
    </section>
  );
}
