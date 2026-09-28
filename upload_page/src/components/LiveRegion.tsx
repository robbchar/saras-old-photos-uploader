// A visually-hidden aria-live region for polite screen-reader
// announcements (run progress, endings). Presentational only - Task 14's
// App decides what message to pass and when it changes.

export interface LiveRegionProps {
  message: string;
}

export function LiveRegion({ message }: LiveRegionProps) {
  return (
    <div role="status" aria-live="polite" className="sr-only">
      {message}
    </div>
  );
}
