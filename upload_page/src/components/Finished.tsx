// The run's final screen. Ending is a discriminated union of five distinct
// outcomes (see schemas.ts) and each one gets its own wording - a plain
// "the run ended" message would bury the one thing the operator actually
// needs to know (did it succeed, did it stop, is IA rate-limiting us, was
// it refused outright, or did it end with nothing to report at all).

import type { Ending, RowFailure, Summary } from "../api/schemas";

export interface FinishedProps {
  ending: Ending;
  onChooseAnother: () => void;
}

function RowFailureList({
  title,
  items,
  tone,
}: {
  title: string;
  items: RowFailure[];
  tone: "danger" | "neutral";
}) {
  const itemClassName =
    tone === "danger"
      ? "rounded border border-danger-border bg-danger-bg px-2 py-1 text-sm text-danger"
      : "rounded border border-border px-2 py-1 text-sm text-text";
  return (
    <div className="mt-4">
      <h3 className="text-sm font-semibold text-muted">{`${title} (${items.length})`}</h3>
      {items.length === 0 ? (
        <p className="text-muted">None</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {items.map((item, index) => (
            <li key={`${item.identifier}-${index}`} className={itemClassName}>
              <span className="font-mono">{item.identifier}</span>
              <span className="ml-2">{item.error}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** The stopped ending's headline: "Stopped after N of M" when the run's
 * planned total was learned (a run_header was written), or just
 * "Stopped after N" when it wasn't - stopping before the total is known is
 * itself possible (see page_runs.Stopped), and "of planned" read as a
 * literal, confusing word in that case rather than a placeholder. */
function stoppedHeadline(succeeded: number, planned: number | null): string {
  return planned === null ? `Stopped after ${succeeded}` : `Stopped after ${succeeded} of ${planned}`;
}

/** The failures/unconfirmed/skipped/not_attempted breakdown shared by
 * every ending that carries a Summary (completed, stopped, rate_limited). */
function SummaryBreakdown({ summary }: { summary: Summary }) {
  return (
    <>
      <RowFailureList title="Failed" items={summary.failures} tone="danger" />
      <RowFailureList title="Unconfirmed" items={summary.unconfirmed} tone="neutral" />
      <RowFailureList title="Skipped" items={summary.skipped} tone="neutral" />
      <p className="mt-4 text-muted">{`Not attempted: ${summary.not_attempted}`}</p>
    </>
  );
}

function FinishedBody({ ending }: { ending: Ending }) {
  switch (ending.kind) {
    case "completed":
      return (
        <>
          <p className="font-semibold text-text">
            {`${ending.summary.succeeded} uploaded, ${ending.summary.failures.length} failed`}
          </p>
          <SummaryBreakdown summary={ending.summary} />
        </>
      );
    case "stopped":
      return (
        <>
          <p className="font-semibold text-text">
            {stoppedHeadline(ending.summary.succeeded, ending.planned)}
          </p>
          <SummaryBreakdown summary={ending.summary} />
        </>
      );
    case "rate_limited":
      return (
        <>
          <p className="font-semibold text-danger">
            Internet Archive asked us to slow down — try again later
          </p>
          <SummaryBreakdown summary={ending.summary} />
        </>
      );
    case "refused":
      return (
        <div className="font-mono text-sm text-danger">
          {ending.reason_lines.map((line, index) => (
            <p key={index}>{line}</p>
          ))}
        </div>
      );
    case "ended_without_summary":
      return (
        <p className="text-muted">
          ended without a summary; run the same theme again to pick up where it stopped
        </p>
      );
    default: {
      const exhaustiveCheck: never = ending;
      return exhaustiveCheck;
    }
  }
}

export function Finished({ ending, onChooseAnother }: FinishedProps) {
  return (
    <section className="rounded border border-border bg-surface p-4 text-text">
      <FinishedBody ending={ending} />
      <button
        type="button"
        onClick={onChooseAnother}
        className="mt-6 rounded bg-accent px-4 py-2 text-on-accent hover:bg-accent-hover"
      >
        Choose another theme
      </button>
    </section>
  );
}
