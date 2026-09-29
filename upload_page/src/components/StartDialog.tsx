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
  const items = count === 1 ? "item" : "items";
  return (
    <Dialog.Root>
      <Dialog.Trigger className="btn-primary">
        {`Upload ${count} ${items} to Internet Archive`}
      </Dialog.Trigger>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 bg-overlay" />
        <Dialog.Content className="fixed left-1/2 top-1/2 w-96 -translate-x-1/2 -translate-y-1/2 rounded border border-border-strong bg-raised p-6 text-text shadow-lg">
          <Dialog.Title className="caps text-base tracking-[0.08em]">Start this upload?</Dialog.Title>
          <Dialog.Description className="mt-2 text-muted">
            {`This sends ${count} ${items} from "${batch}" to Internet Archive.`}
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
                className="btn-secondary"
              >
                Cancel
              </button>
            </Dialog.Close>
            <Dialog.Close asChild>
              <button
                type="button"
                onClick={onConfirm}
                className="btn-primary"
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
