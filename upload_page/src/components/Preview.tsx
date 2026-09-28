// The preview screen for one theme: what is ready to send, what still
// needs a human, and what has never been catalogued at all. App re-fetches
// the underlying ValidateDoc on Re-check; this component never refreshes
// on its own.

import type { ValidateDoc } from "../api/schemas";
import { RowBreakdown } from "./RowBreakdown";

export interface PreviewProps {
  doc: ValidateDoc;
  checkedAt: string;
  onRecheck: () => void;
}

function formatCheckedAt(checkedAt: string): string {
  const parsed = new Date(checkedAt);
  const hours = String(parsed.getHours()).padStart(2, "0");
  const minutes = String(parsed.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

export function Preview({ doc, checkedAt, onRecheck }: PreviewProps) {
  const rows = doc.rows ?? [];
  // `doc.ready_to_upload` is the authoritative ready-to-upload count (it
  // excludes already-done rows via is_upload_target), so it is used directly
  // rather than recounting "ready" verdicts, which would wrongly include rows
  // that are ready but already sent.
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

      <p className="mt-4 font-semibold text-text">{`${doc.ready_to_upload} ready to upload`}</p>

      <RowBreakdown rows={rows} />
    </section>
  );
}
