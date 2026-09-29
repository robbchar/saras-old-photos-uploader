// The page's top banner: which project/collection this is, and - the one
// thing that must never be missed - whether uploads here are real. Mode
// is conveyed by text, never by color alone, so the test-mode styling
// below is a reinforcement, not the signal itself.

import type { ReactNode } from "react";

export interface HeaderProps {
  project: string;
  collection: string;
  live: boolean;
  /** Page-level controls (the color-scheme toggle), top right. */
  toolbar?: ReactNode;
  /** The persistent theme picker lives in the header; App passes it here so
   * this component stays presentational and owns no picker state. */
  children?: ReactNode;
}

const TEST_MODE_MESSAGE =
  "TEST MODE — uploads go to test_collection and expire in about 30 days";

export function Header({ project, collection, live, toolbar, children }: HeaderProps) {
  return (
    <header className="rounded border border-t-[3px] border-border border-t-brand bg-raised px-4 py-3">
      <div className="flex items-center gap-3">
        <div className="min-w-0">
          <p className="caps text-[11px] tracking-[0.14em] text-muted">Lower Columbia Preservation Society</p>
          <p className="flex flex-wrap items-baseline gap-x-2">
            <span className="caps text-xl font-bold tracking-[0.06em] text-text">{project}</span>
            <span className="text-sm text-muted">{collection}</span>
          </p>
        </div>
        {toolbar && <div className="ml-auto shrink-0">{toolbar}</div>}
      </div>
      {!live && (
        <p
          role="status"
          className="mt-2 inline-block rounded-full border border-test-border bg-test-bg px-3 py-1 text-sm text-test"
        >
          {TEST_MODE_MESSAGE}
        </p>
      )}
      {children && <div className="mt-3">{children}</div>}
    </header>
  );
}
