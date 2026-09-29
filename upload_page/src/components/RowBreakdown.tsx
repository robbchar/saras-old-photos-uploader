// The three per-row sections of a theme: what has been uploaded, what needs
// fixing (invalid), and what is not yet catalogued (missing fields). Shared by
// the Preview (before a run) and the Finished screen (after one) so both show
// the same breakdown for a theme.

import type { ValidateRow } from "../api/schemas";
import { UploadedList } from "./UploadedList";

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

export interface RowBreakdownProps {
  rows: ValidateRow[];
}

export function RowBreakdown({ rows }: RowBreakdownProps) {
  const needsFixing = compressToRanges(
    rows.filter((row) => row.verdict === "invalid"),
    reasonForInvalidRow,
  );
  const notCatalogued = compressToRanges(
    rows.filter((row) => row.verdict === "not_ready"),
    reasonForNotReadyRow,
  );

  return (
    <>
      <UploadedList rows={rows} />

      <div className="mt-4">
        <h3 className="caps text-xs text-muted">Needs fixing</h3>
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
        <h3 className="caps text-xs text-muted">Not yet catalogued</h3>
        {notCatalogued.length === 0 ? (
          <p className="text-muted">None</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {notCatalogued.map((range) => (
              <li
                key={`${range.start}-${range.end}`}
                className="rounded border border-border-strong bg-raised px-2 py-1 font-mono text-sm text-text"
              >
                {formatRange(range)}
              </li>
            ))}
          </ul>
        )}
      </div>
    </>
  );
}
