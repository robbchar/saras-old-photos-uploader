// The collapsible list of a theme's already-uploaded rows, by filename. Shared
// by the Preview (before a run) and the Finished screen (after one) so both
// show the same "Uploaded (N)" section for a theme.

import type { ValidateRow } from "../api/schemas";

export interface UploadedListProps {
  /** All of a theme's rows; the already-uploaded ones (state === "done") are
   * the ones listed. */
  rows: ValidateRow[];
}

export function UploadedList({ rows }: UploadedListProps) {
  const uploaded = rows.filter((row) => row.state === "done");
  return (
    <details className="mt-4">
      <summary className="caps cursor-pointer text-xs text-muted">
        {`Uploaded (${uploaded.length})`}
      </summary>
      {uploaded.length === 0 ? (
        <p className="mt-1 text-muted">None</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {uploaded.map((row) => (
            <li
              key={row.row}
              className="rounded border border-border-strong bg-raised px-2 py-1 font-mono text-sm text-text"
            >
              {row.file || row.identifier || `row ${row.row}`}
              {row.verdict === "held" ? " (withdrawn)" : ""}
            </li>
          ))}
        </ul>
      )}
    </details>
  );
}
