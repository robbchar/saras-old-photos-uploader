// The page's top banner: which project/collection this is, and - the one
// thing that must never be missed - whether uploads here are real. Mode
// is conveyed by text, never by color alone, so the amber styling below
// is a reinforcement, not the signal itself.

export interface HeaderProps {
  project: string;
  collection: string;
  live: boolean;
}

const TEST_MODE_MESSAGE =
  "TEST MODE — uploads go to test_collection and expire in about 30 days";

export function Header({ project, collection, live }: HeaderProps) {
  return (
    <header className="border-b border-border bg-surface px-4 py-3">
      <div className="flex items-baseline gap-2 font-sans">
        <span className="font-semibold text-text">{project}</span>
        <span className="text-muted">{collection}</span>
      </div>
      {!live && (
        <p
          role="status"
          className="mt-2 rounded border border-test-border bg-test-bg px-3 py-2 text-sm text-test"
        >
          {TEST_MODE_MESSAGE}
        </p>
      )}
    </header>
  );
}
