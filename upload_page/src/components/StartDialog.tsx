// The final confirmation before a run starts. The trigger IS the view's
// one primary action (accent) - opening it costs nothing; only Confirm
// inside the dialog actually starts anything.

import { Dialog } from "radix-ui";

export interface StartDialogProps {
  count: number;
  batch: string;
  live: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function StartDialog({ count, batch, live, onConfirm, onCancel }: StartDialogProps) {
  return (
    <Dialog.Root>
      <Dialog.Trigger className="rounded bg-accent px-4 py-2 font-sans text-on-accent hover:bg-accent-hover">
        {`Upload ${count} photos to Internet Archive`}
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-black/40" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-96 -translate-x-1/2 -translate-y-1/2 rounded border border-border bg-raised p-6 text-text">
          <Dialog.Title className="text-lg font-semibold">Start this upload?</Dialog.Title>
          <Dialog.Description className="mt-2 text-muted">
            {`This sends ${count} photos from "${batch}" to Internet Archive.`}
          </Dialog.Description>
          {live && (
            <p className="mt-3 rounded border border-danger-border bg-danger-bg px-3 py-2 text-sm text-danger">
              Once uploaded, these items cannot be undone or renamed.
            </p>
          )}
          <div className="mt-6 flex justify-end gap-2">
            <Dialog.Close asChild>
              <button
                type="button"
                onClick={onCancel}
                className="rounded border border-border-strong px-4 py-2 text-text"
              >
                Cancel
              </button>
            </Dialog.Close>
            <Dialog.Close asChild>
              <button
                type="button"
                onClick={onConfirm}
                className="rounded bg-accent px-4 py-2 text-on-accent hover:bg-accent-hover"
              >
                Confirm
              </button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
