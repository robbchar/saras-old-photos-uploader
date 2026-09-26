// The preview screen for one theme: what is ready to send, what still
// needs a human, and what has never been catalogued at all. App re-fetches
// the underlying ValidateDoc on Re-check; this component never refreshes
// on its own.

import type { ValidateDoc, ValidateRow } from "../api/schemas";

export interface PreviewProps {
  doc: ValidateDoc;
  checkedAt: string;
  onRecheck: () => void;
}

interface RowRange {
  start: number;
  end: number;
  reason: string;
}

function reasonForInvalidRow(row: ValidateRow): string {
  // "invalid" rows are supposed to always carry at least one operator-facing
  // error string (that is what makes them invalid) - see ia_bulk.py's
  // validate --json - but errors[0] is never guaranteed by the type, so a
  // malformed row falls back to a plain label instead of rendering
  // "undefined".
  return row.errors[0] ?? "invalid";
}

function reasonForNotReadyRow(row: ValidateRow): string {
  return `needs ${row.missing_fields.join(", ")}`;
}

/** Sorts by row number, then folds consecutive rows sharing the exact
 * same reason into one range. A run breaks whenever the next row isn't
 * immediately contiguous, or its reason differs. */
function compressToRanges(rows: ValidateRow[], reasonFor: (row: ValidateRow) => string): RowRange[] {
  const sorted = [...rows].sort((a, b) => a.row - b.row);
  const ranges: RowRange[] = [];
  for (const row of sorted) {
    const reason = reasonFor(row);
    const previous = ranges.at(-1);
    if (previous && previous.reason === reason && row.row === previous.end + 1) {
      previous.end = row.row;
    } else {
      ranges.push({ start: row.row, end: row.row, reason });
    }
  }
  return ranges;
}

function formatRange(range: RowRange): string {
  const rowLabel = range.start === range.end ? `row ${range.start}` : `rows ${range.start}-${range.end}`;
  return `${rowLabel}: ${range.reason}`;
}

function formatCheckedAt(checkedAt: string): string {
  const parsed = new Date(checkedAt);
  const hours = String(parsed.getHours()).padStart(2, "0");
  const minutes = String(parsed.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

export function Preview({ doc, checkedAt, onRecheck }: PreviewProps) {
  const rows = doc.rows ?? [];
  const readyCount = rows.filter((row) => row.verdict === "ready").length;
  const needsFixing = compressToRanges(
    rows.filter((row) => row.verdict === "invalid"),
    reasonForInvalidRow,
  );
  const notCatalogued = compressToRanges(
    rows.filter((row) => row.verdict === "not_ready"),
    reasonForNotReadyRow,
  );

  return (
    <section className="rounded border border-border bg-surface p-4 text-text">
      <header className="flex items-center justify-between gap-4">
        <span className="text-muted">{`checked at ${formatCheckedAt(checkedAt)}`}</span>
        <button
          type="button"
          onClick={onRecheck}
          className="rounded border border-border-strong px-3 py-1.5 text-text"
        >
          Re-check
        </button>
      </header>

      <p className="mt-4 font-semibold text-text">{`${readyCount} ready to upload`}</p>

      <div className="mt-4">
        <h3 className="text-sm font-semibold text-muted">Needs fixing</h3>
        {needsFixing.length === 0 ? (
          <p className="text-muted">None</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {needsFixing.map((range) => (
              <li
                key={`${range.start}-${range.end}`}
                className="rounded border border-danger-border bg-danger-bg px-2 py-1 font-mono text-sm text-danger"
              >
                {formatRange(range)}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mt-4">
        <h3 className="text-sm font-semibold text-muted">Not yet catalogued</h3>
        {notCatalogued.length === 0 ? (
          <p className="text-muted">None</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {notCatalogued.map((range) => (
              <li
                key={`${range.start}-${range.end}`}
                className="rounded border border-border px-2 py-1 font-mono text-sm text-text"
              >
                {formatRange(range)}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
