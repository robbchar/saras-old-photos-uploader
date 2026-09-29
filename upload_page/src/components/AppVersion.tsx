// The tool's version, pinned to the window's bottom-right corner. App pads
// the page bottom so this never covers the last line of content.

export interface AppVersionProps {
  version: string;
}

export function AppVersion({ version }: AppVersionProps) {
  return (
    <p
      title={`Upload tool version ${version}`}
      className="fixed right-3 bottom-2 rounded bg-bg/85 px-1.5 font-mono text-[11px] text-muted"
    >
      v{version}
    </p>
  );
}
